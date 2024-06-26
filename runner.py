import datetime
import json

from aws_requests_auth.aws_auth import AWSRequestsAuth

import pymysql.connections
from sqlalchemy.ext.declarative import declarative_base
from google.cloud.sql.connector import Connector
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine, Column, String, Integer, DateTime, ForeignKey, select, insert, and_, or_
import argparse
import os
import requests
import logging

session = None
connector = Connector()
Model = declarative_base(name='Model')

LIST_DIRECTORIES_URL = 'https://j6yq55awhm4zltnzwiov7m6dvy0guuvi.lambda-url.us-east-1.on.aws/'
GET_DIRECTORY_FILES_URL = 'https://mrurqi3b2wc7unxcu7hsptsmmm0ccpwg.lambda-url.us-east-1.on.aws/'
PROCESS_FILES_URL = 'https://t22goxl5ubsigzprq2ru2pezxm0zxdug.lambda-url.us-east-1.on.aws/'

AWS_ACCESS_KEY = None
AWS_SECRET_KEY = None

# TODO: add command line arguments to switch between medrxiv and biorxiv for source and destination buckets

# region models
class Directory(Model):
    __tablename__ = 'directories'
    id = Column(Integer, primary_key=True)
    path = Column(String(250))
    scanned_dt = Column(DateTime)

    def __init__(self, path, scanned_dt=None):
        self.path = path
        if scanned_dt:
            self.scanned_dt = scanned_dt


class File(Model):
    __tablename__ = 'files'
    id = Column(Integer, primary_key=True)
    archive_filename = Column(String(100))
    xml_filename = Column(String(100))
    parent_directory = Column(Integer, ForeignKey('directories.id'))
    status = Column(String(50))

    def __init__(self, anID, archive_filename, xml_filename, parent_directory, status):
        self.id = anID
        self.archive_filename = archive_filename
        self.xml_filename = xml_filename
        self.parent_directory = parent_directory
        self.status = status


class FileEvent(Model):
    __tablename__ = 'file_events'
    id = Column(Integer, primary_key=True)
    file_id = Column(Integer, ForeignKey('file.id'))
    event_type = Column(String(50))
    event_dt = Column(DateTime)

    def __init__(self, anID, file_id, event_type, event_dt):
        self.id = anID
        self.file_id = file_id
        self.event_type = event_type
        self.event_dt = event_dt

# endregion


def init_db(instance: str, user: str, password: str, database: str) -> None:
    def get_conn() -> pymysql.connections.Connection:
        conn: pymysql.connections.Connection = connector.connect(
            instance_connection_string=instance,
            driver='pymysql',
            user=user,
            password=password,
            database=database
        )
        return conn

    engine = create_engine('mysql+pymysql://', creator=get_conn, echo=False)
    global session
    session = sessionmaker()
    session.configure(bind=engine)


def update_directories_table():
    path_list = []
    existing_directories = session.execute(select(Directory)).all()
    existing_paths = [x[0].path for x in existing_directories]
    months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
    print('getting current content by months')
    for month in months:
        payload_c = {'bucket': 'medrxiv-src-monthly', 'subdirectory': 'Current_Content/' + month}
        r = get_with_auth(LIST_DIRECTORIES_URL, payload_c)
        if r.status_code == 200:
            response_json = r.json()
            # print(len(response_json['directories']))
            path_list.extend(response_json['directories'])
        else:
            print(r.status_code)
            print(r.text)
    insert_buffer = []
    print(len(path_list))
    for path in path_list:
        if path not in existing_paths:
            insert_buffer.append({'path': path})
    print(len(insert_buffer))
    payload_b = {'bucket': 'medrxiv-src-monthly', 'subdirectory': 'Back_Content/'}
    print('getting back content')
    r = get_with_auth(LIST_DIRECTORIES_URL, payload_b)
    if r.status_code == 200:
        response_json = r.json()
        path_list = response_json['directories']
    else:
        print(r.status_code)
        print(r.text)
    print(len(path_list))
    for path in path_list:
        if path not in existing_paths:
            insert_buffer.append({'path': path})
    print(len(insert_buffer))
    session.bulk_insert_mappings(Directory, insert_buffer)
    session.commit()


def scan_new_directories(update_date=None):
    if not update_date:
        existing_directories = session.execute(
            select(Directory)
            .where(Directory.scanned_dt.is_(None))
        ).all()
    else:
        existing_directories = session.execute(
            select(Directory)
            .where(
                or_(Directory.scanned_dt.is_(None), Directory.scanned_dt < update_date))
        ).all()
    current_files = set([filename for filename, in session.execute(select(File.archive_filename)).all()])
    logging.debug(f"{update_date}\t{len(existing_directories)}\t{len(current_files)}")
    insert_buffer = []
    for directory, in existing_directories:
        logging.info('Working on ' + directory.path)
        if directory.path == 'Back_Content/' or directory.path == 'Current_Content/':
            directory.scanned_dt = datetime.datetime.now()
            continue
        payload = {'source-bucket': 'medrxiv-src-monthly', 'directory-prefix': directory.path}
        response = get_with_auth(GET_DIRECTORY_FILES_URL, payload)
        if response.status_code == 200:
            response_json = response.json()
            if response_json['file_count'] > 0:
                path_list = response_json['paths']
                logging.info(f"{len(path_list)} filepaths in this directory")
                for path in path_list:
                    if path not in current_files:
                        insert_buffer.append(
                            {
                                'archive_filename': path,
                                'parent_directory': directory.id,
                                'status': 'discovered'
                            }
                        )
        else:
            logging.debug(json.dumps(payload))
            logging.warning(response.status_code)
            logging.warning(response.text)
        directory.scanned_dt = datetime.datetime.now()
        if len(insert_buffer) > 0:
            logging.info(f"Inserting {len(insert_buffer)} records")
            session.bulk_insert_mappings(File, insert_buffer)
            insert_buffer.clear()
        session.commit()
    if len(insert_buffer) > 0:
        session.bulk_insert_mappings(File, insert_buffer)
    session.commit()


def update_current_month(partition_size, key, secret):
    current_month_directory_path = datetime.datetime.now().strftime('Current_Content/%B_%Y/')
    current_month_directory, = session.execute(
        select(Directory)
        .where(Directory.path == current_month_directory_path)
    ).first()
    if not current_month_directory:
        session.execute(insert(Directory).values(path=current_month_directory_path))
        current_month_directory, = session.execute(
            select(Directory)
            .where(Directory.path == current_month_directory_path)
        ).first()

    scan_directory(current_month_directory)
    process_directory_files(current_month_directory, partition_size, key, secret)


def scan_directory(directory):
    payload = {'source-bucket': 'medrxiv-src-monthly', 'directory-prefix': directory.path}
    response = get_with_auth(GET_DIRECTORY_FILES_URL, payload)
    insert_buffer = []
    if response.status_code == 200:
        response_json = response.json()
        if response_json['file_count'] > 0:
            path_list = response_json['paths']
            for path in path_list:
                insert_buffer.append(
                    {
                        'archive_filename': path,
                        'parent_directory': directory.id,
                        'status': 'discovered'
                    }
                )
    else:
        print(response.status_code)
        print(response.text)
    directory.scanned_dt = datetime.datetime.now()
    if len(insert_buffer) > 0:
        session.bulk_insert_mappings(File, insert_buffer)
    session.commit()


def process_directory_files(directory, partition_size, key, secret):
    files_to_process = session.execute(
        select(File)
        .where(
            and_(File.status != 'downloaded', File.parent_directory == directory.id)
        )).all()
    file_dict = {}
    for file, in files_to_process:
        file_dict[file.archive_filename] = file
    filename_list = [fileobj.archive_filename for fileobj, in files_to_process]
    data = {
        'source-bucket': 'medrxiv-src-monthly',
        'paths': [],
        'destination': 'translator-text-workflow-dev_work',
        'directory': 'medrxiv-xml/',
        'key_id': key,
        'secret': secret
    }
    while len(filename_list) > 0:
        sublist = filename_list[:partition_size] if len(filename_list) > partition_size else filename_list
        data['paths'] = sublist
        response = get_with_auth(PROCESS_FILES_URL, data)
        if response.status_code == 200:
            response_json = response.json()
            new_events_buffer = []
            completed_filename_list = []
            downloaded_files_dict = response_json['downloaded_files']
            error_files_list = response_json['error_files']
            print(f"{len(downloaded_files_dict)} succeeded, {len(error_files_list)} errors")
            print(f"Time elapsed: {response_json['runtime']}ms")
            for archive_filename in downloaded_files_dict.keys():
                completed_filename_list.append(archive_filename)
                file_object = file_dict[archive_filename]
                xml_filename = downloaded_files_dict[archive_filename]
                file_object.xml_filename = xml_filename
                file_object.status = 'downloaded'
                new_events_buffer.append({
                    'file_id': file_object.id,
                    'event_type': 'downloaded',
                    'event_dt': datetime.datetime.now()
                })
            for archive_filename in error_files_list:
                completed_filename_list.append(archive_filename)
                file_object = file_dict[archive_filename]
                file_object.status = 'error'
                new_events_buffer.append({
                    'file_id': file_object.id,
                    'event_type': 'error',
                    'event_dt': datetime.datetime.now()
                })
            for filename in completed_filename_list:
                filename_list.remove(filename)
            session.commit()
        else:
            print(response.status_code)
            print(response.text)


def process_files_by_parts(partition_size, key, secret):
    files_to_process = session.execute(
        select(File).where(and_(File.status != 'downloaded', File.status != 'error')).limit(50000)).all()
    file_dict = {}
    logging.info(f'Found {len(files_to_process)} files to process')
    for file, in files_to_process:
        file_dict[file.archive_filename] = file
    filename_list = [fileobj.archive_filename for fileobj, in files_to_process]
    data = {
        'source-bucket': 'medrxiv-src-monthly',
        'paths': [],
        'destination': 'translator-text-workflow-dev_work',
        'directory': 'medrxiv-xml/',
        'key_id': key,
        'secret': secret
    }
    while len(filename_list) > 0:
        sublist = filename_list[:partition_size] if len(filename_list) > partition_size else filename_list
        data['paths'] = sublist
        response = get_with_auth(PROCESS_FILES_URL, data)
        if response.status_code == 200:
            response_json = response.json()
            new_events_buffer = []
            completed_filename_list = []
            downloaded_files_dict = response_json['downloaded_files']
            error_files_list = response_json['error_files']
            logging.info(f"{len(downloaded_files_dict)} succeeded, {len(error_files_list)} errors")
            logging.debug(f"Time elapsed: {response_json['runtime']}ms")
            for archive_filename in downloaded_files_dict.keys():
                completed_filename_list.append(archive_filename)
                file_object = file_dict[archive_filename]
                xml_filename = downloaded_files_dict[archive_filename]
                file_object.xml_filename = xml_filename
                file_object.status = 'downloaded'
                new_events_buffer.append({
                    'file_id': file_object.id,
                    'event_type': 'downloaded',
                    'event_dt': datetime.datetime.now()
                })
            for archive_filename in error_files_list:
                completed_filename_list.append(archive_filename)
                file_object = file_dict[archive_filename]
                file_object.status = 'error'
                new_events_buffer.append({
                    'file_id': file_object.id,
                    'event_type': 'error',
                    'event_dt': datetime.datetime.now()
                })
            for filename in completed_filename_list:
                filename_list.remove(filename)
            session.commit()
        else:
            logging.warning(response.status_code)
            logging.warning(response.text)


def get_with_auth(url, payload):
    host = url.replace('https:', '').replace('/', '')
    auth = AWSRequestsAuth(aws_access_key=AWS_ACCESS_KEY,
                           aws_secret_access_key=AWS_SECRET_KEY,
                           aws_host=host,
                           aws_region='us-east-1',
                           aws_service='lambda')
    headers = {"Content-type": "application/json"}
    response = requests.get(url, auth=auth, json=payload, headers=headers, timeout=(5, 600))
    # print(response.content)
    return response


if __name__ == "__main__":
    logging.basicConfig(format='%(asctime)s %(module)s:%(funcName)s:%(levelname)s: %(message)s', level=logging.DEBUG)
    parser = argparse.ArgumentParser()
    parser.add_argument('-i', '--instance', help='GCP DB instance name')
    parser.add_argument('-d', '--database', help='database name')
    parser.add_argument('-u', '--user', help='database username')
    parser.add_argument('-p', '--password', help='database password')
    parser.add_argument('-k', '--key', help='HMAC key id')
    parser.add_argument('-s', '--secret', help='HMAC secret')
    parser.add_argument('-a', '--aws-key', help='AWS access key')
    parser.add_argument('-w', '--aws-secret', help='AWS secret key')
    parser.add_argument('-t', '--task', help='task to execute')
    parser.add_argument('-o', '--old', help='previous scan date to rescan (ISO 8601 date)')
    parser.add_argument('-c', '--chunk', help='size of file processing chunks', type=int)
    args = parser.parse_args()
    init_db(
        instance=args.instance if args.instance else os.getenv('MYSQL_DATABASE_INSTANCE', None),
        user=args.user if args.user else os.getenv('MYSQL_DATABASE_USER', None),
        password=args.password if args.password else os.getenv('MYSQL_DATABASE_PASSWORD', None),
        database=args.database if args.database else 'biorxiv'
    )
    task = args.task if args.task else 'all'
    hmac_key = args.key if args.key else os.getenv("HMAC_KEY_ID", None)
    hmac_secret = args.secret if args.secret else os.getenv("HMAC_SECRET", None)
    AWS_ACCESS_KEY = args.aws_key if args.aws_key else os.getenv("AWS_ACCESS_KEY", None)
    AWS_SECRET_KEY = args.aws_secret if args.aws_secret else os.getenv("AWS_SECRET_KEY", None)
    os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = 'prod-creds.json'
    chunk_size = args.chunk if args.chunk else 100
    session = session()

    if task == 'ls' or task == 'all':
        update_directories_table()
    if task == 'scan' or task == 'all':
        old_date = datetime.datetime.fromisoformat(args.old) if args.old else datetime.datetime.now()
        scan_new_directories(old_date)
    if task == 'process' or task == 'all':
        process_files_by_parts(chunk_size, hmac_key, hmac_secret)
    if task == 'update' or task == 'all':
        update_current_month(chunk_size, hmac_key, hmac_secret)
