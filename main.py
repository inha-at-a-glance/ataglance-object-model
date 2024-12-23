from tools.demo import make_parser, main as detection
from yolox.exp import get_exp
import boto3
import pymysql
import os
import json
import time
from moviepy.video.io.ffmpeg_tools import ffmpeg_extract_subclip
from botocore.exceptions import NoCredentialsError

s3_client = boto3.client('s3')
S3_BUCKET_NAME = 'ataglance-bucket'

# RDS 연결 정보 불러오기
'''
RDS_USERNAME = os.getenv('RDS_USERNAME')
RDS_PASSWORD = os.getenv('RDS_PASSWORD')
RDS_HOST = os.getenv('RDS_HOST')
RDS_DB = os.getenv('RDS_DB')
'''
RDS_HOST = "ataglance-db.czwmu86ms4yl.us-east-1.rds.amazonaws.com"
RDS_USERNAME = "admin"
RDS_PASSWORD = "ataglance12!"
RDS_DB = "ataglance"
RDS_PORT = 3306

def fetch_news_from_db(news_id):
    """
    DB에서 뉴스 데이터를 가져옵니다.
    :param news_id: 뉴스 ID
    :return: 뉴스 데이터 목록 (id, timestamp JSON 형식 포함)
    """
    try:
        connection = pymysql.connect(
            host=RDS_HOST,
            user=RDS_USERNAME,
            password=RDS_PASSWORD,
            database=RDS_DB,
            port=RDS_PORT,
            cursorclass=pymysql.cursors.DictCursor
        )
        cursor = connection.cursor()
        cursor.execute("SELECT news_id, text_id, time_stamp FROM TextProc WHERE news_id = %s", (news_id,))
        return cursor.fetchall()
    except pymysql.MySQLError as e:
        print(f"데이터베이스 연결 오류: {e}")
    finally:
        cursor.close()
        connection.close()

def download_video_from_s3(news_id, local_path):
    """
    S3에서 영상을 다운로드합니다.
    :param news_id: 뉴스 ID
    :param local_path: 로컬 저장 경로
    """
    object_key = f"{news_id}/{news_id}.mp4"  # S3 경로
    try:
        s3_client.download_file(S3_BUCKET_NAME, object_key, local_path)
        print(f"S3에서 {object_key} 다운로드 완료.")
    except NoCredentialsError:
        print("AWS 자격 증명이 설정되지 않았습니다.")
    except Exception as e:
        print(f"S3 다운로드 오류: {e}")

def split_video_by_timestamps(news_id, timestamps, segment_dir):
    """
    JSON으로 제공된 타임스탬프를 기반으로 영상을 분할합니다.
    :param news_id: 뉴스 ID
    :param timestamps: JSON 형식의 타임스탬프 (3쌍의 시작 및 종료 시간)
    :param segment_dir: 분할된 파일을 저장할 디렉토리
    :return: 분할된 파일 경로 목록
    """
    video_path = f"videos/{news_id}.mp4"
    segment_paths = []
    timestamp=timestamps['time']

    for i in range(len(timestamp)):
        segment_start = round(timestamp[i][0]/1000)
        segment_end = round(timestamp[i][1]/1000)
        segment_path = os.path.join(segment_dir, f"segment_{i + 1}.mp4")
        ffmpeg_extract_subclip(video_path, segment_start, segment_end, targetname=segment_path)
        segment_paths.append(segment_path)
        print(f"영상 분할 완료: {segment_path}")

    return segment_paths

def process_segments_with_yolox(segment_paths):
    """
    YOLOX로 분할된 영상에서 객체를 탐지하고 이미지를 저장합니다.
    :param segment_paths: 분할된 영상 경로 목록
    :return: 저장된 이미지 파일 경로 목록
    """
    # YOLOX 모델을 사용해 객체 탐지
    image_paths=[]
    for segment_path in segment_paths:
        args = make_parser().parse_args(['video', '-n', 'yolox-s', '-c', 'best_ckpt.pth',
                                         '--path', segment_path, '--conf', '0.75', '--nms', '0.5',
                                         '--tsize', '640', '--save_result', '--device', 'cpu'])
        exp = get_exp(args.exp_file, args.name)

        res_folder = detection(exp, args)
        file_list = os.listdir(res_folder)
        file_list_jpg=[]
        for file in file_list:
            if file.endswith('.jpg'):
                file_list_jpg.append(os.path.join(res_folder, file))
        image_paths.append(file_list_jpg)

    return image_paths

def upload_images_to_s3(news_id, image_paths):
    """
    감지된 객체 이미지를 S3에 업로드합니다.
    :param news_id: 뉴스 ID
    :param image_paths: 로컬 이미지 파일 경로 목록
    :return: S3에 업로드된 이미지 경로 목록
    """
    s3_paths = []

    for i in range(len(image_paths)):
        image_path = image_paths[i]
        s3_path = []
        for j in range(len(image_path)):
            object_key = f"{news_id}/page_{i+1}/{os.path.basename(image_path[j])}"
            try:
                s3_client.upload_file(image_path[j], S3_BUCKET_NAME, object_key)
                s3_path.append(f"s3://{S3_BUCKET_NAME}/{object_key}")
                print(f"S3 업로드 완료: {object_key}")
            except Exception as e:
                print(f"S3 업로드 오류: {e}")
        s3_paths.append(s3_path)

    return s3_paths

def save_metadata_to_rds(news_id, text_id, s3_paths):
    """
    S3 이미지 경로를 RDS에 저장합니다.
    :param news_id: 뉴스 ID
    :param s3_paths: S3 이미지 경로 목록
    """
    try:
        connection = pymysql.connect(
            host=RDS_HOST,
            user=RDS_USERNAME,
            password=RDS_PASSWORD,
            database=RDS_DB,
            port=RDS_PORT,
            cursorclass=pymysql.cursors.DictCursor
        )
        cursor = connection.cursor()

        s3_paths_json=json.dumps({"image_path":s3_paths})
        now=time.strftime('%Y-%m-%d %H:%M:%S')
        sql = "INSERT INTO VideoProc (news_id, text_id, objects_path, created_at, updated_at) VALUES (%s, %s, %s, %s, %s)"
        cursor.execute(sql, (news_id, text_id, s3_paths_json, now, now))

        connection.commit()
        print(f"RDS에 메타데이터 저장 완료: {news_id}")
    except pymysql.MySQLError as e:
        print(f"데이터베이스 연결 오류: {e}")
    finally:
        cursor.close()
        connection.close()

def main():
    news_data = fetch_news_from_db(news_id=1115)
    for news in news_data:
        news_id = news['news_id']
        text_id = news['text_id']
        timestamps = json.loads(news['time_stamp'])  # JSON 파싱 (3쌍의 시작 및 종료 시간)

        # 1. S3에서 영상 다운로드
        download_video_from_s3(news_id, f"videos/{news_id}.mp4")

        # 2. 영상 분할
        segment_dir = f"segments/{news_id}"
        os.makedirs(segment_dir, exist_ok=True)
        segment_paths = split_video_by_timestamps(news_id, timestamps, segment_dir)

        # 3. YOLOX 객체 탐지
        image_paths = process_segments_with_yolox(segment_paths)

        # 4. S3 업로드
        s3_paths = upload_images_to_s3(news_id, image_paths)

        # 5. RDS에 메타데이터 저장
        save_metadata_to_rds(news_id, text_id, s3_paths)

if __name__ == "__main__":
    main()
