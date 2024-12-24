import json
import pymysql
import boto3
from pytubefix import YouTube
from pytubefix.cli import on_progress
import requests
import time
import os

# RDS 연결 정보 불러오기
'''
RDS_USER = os.getenv('RDS_USER')
RDS_PASSWORD = os.getenv('RDS_PASSWORD')
RDS_HOST = os.getenv('RDS_HOST')
RDS_DB = os.getenv('RDS_DB')
'''

# S3 설정
S3_BUCKET = 'ataglance-bucket'
s3 = boto3.client('s3')

# 동영상 다운로드 함수
def download_video(source_url,news_id):
    try:
        # 유튜브 동영상 다운로드
        print(f"Initializing YouTube object for URL: {source_url}")
        yt = YouTube(source_url, on_progress_callback=on_progress, use_oauth=True)
        print(f"YouTube object initialized. Video title: {yt.title}")

        video_stream = yt.streams.get_highest_resolution()
        print(f"Video stream fetched: {video_stream.resolution}, {video_stream.mime_type}")

        # 자막 처리
        subtitle_file = f"/tmp/{news_id}.txt"
        captions = yt.captions  # 자막 목록 가져오기

        # 한국어 자막 확인 및 다운로드
        if 'ko' in captions:
            korean_caption = captions.get_by_language_code('ko')
            print("Korean captions found. Downloading...")
            subtitle_text = korean_caption.generate_srt_captions()

            # 자막을 파일로 저장
            with open(subtitle_file, "w", encoding="utf-8") as file:
                file.write(subtitle_text)
            print(f"Korean subtitles saved to {subtitle_file}")
        else:
            print("No Korean captions available.")
            subtitle_file=None

        video_file = f"/tmp/{news_id}.mp4"  # Lambda 내 임시 디렉토리
        print(f"Downloading video to {video_file}...")
        video_stream.download(output_path="/tmp", filename=f"{news_id}.mp4")
        print("Video download complete.")

        return video_file, subtitle_file
    except Exception as e:
        print(f"Error during YouTube video processing: {e}")
        return {
            'statusCode': 500,
            'body': f"Error during YouTube video processing: {e}"
        }

# S3 업로드 함수
def upload_to_s3(file_path, txt_path, news_id):
    try:
        # S3에 업로드
        print(f"Uploading video to S3 bucket: {S3_BUCKET}, key: {news_id}/{news_id}.mp4")
        s3.upload_file(file_path, S3_BUCKET, f"{news_id}/{news_id}.mp4")
        if(txt_path):
            s3.upload_file(txt_path, S3_BUCKET, f"{news_id}/{news_id}.txt")
        s3_url = f"https://{S3_BUCKET}.s3.amazonaws.com/{news_id}/{news_id}.mp4"
        print(f"Video uploaded to S3: {s3_url}")
        return s3_url
    except Exception as e:
        print(f"Error uploading video to S3: {e}")
        return {
            'statusCode': 500,
            'body': f"Error uploading video to S3: {e}"
        }

# RDS 연결 함수
def connect_to_rds():
    try:
        print("Connecting to RDS...")
        conn = pymysql.connect(host=RDS_HOST, user=RDS_USER, password=RDS_PASSWORD, database=RDS_DB)
        print("RDS connection established.")
        return conn
    except Exception as e:
        print(f"Error connecting to RDS: {e}")
        time.sleep(5)
        return None

# RDS Polling 및 처리 함수
def process_rds():
    while True:
        try:
            conn = connect_to_rds()
            if not conn:
                return

            cursor = conn.cursor()

            # 새로운 링크 가져오기
            cursor.execute("SELECT news_id, source_url FROM News where video_file_path is NULL limit 1")
            row = cursor.fetchone()
            if not row:
                print("No pending videos. Waiting...")
                time.sleep(10)  # 대기 후 다시 확인
                continue

            news_id, source_url = row
            print(f"Processing video: {source_url}")

            # 다운로드
            file_path, txt_path = download_video(source_url,news_id)
            if not file_path:
                print(f"Download failed for news_id {news_id}. Marking as failed.")
                cursor.execute("UPDATE News SET video_file_path = 'FAILED' WHERE news_id = %s", (news_id,))
                conn.commit()
                continue

            # S3 업로드
            s3_url = upload_to_s3(file_path, txt_path, news_id)
            if not s3_url:
                print(f"S3 upload failed for news_id {news_id}. Marking as failed.")
                cursor.execute("UPDATE News SET video_file_path = 'FAILED' WHERE news_id = %s", (news_id,))
                conn.commit()
                continue

            # RDS 업데이트
            print(f"Updating RDS for news_id {news_id} with S3 URL {s3_url}")
            cursor.execute("UPDATE News SET video_file_path = %s WHERE news_id = %s", (s3_url, news_id))
            conn.commit()

        except Exception as e:
            print(f"Error during processing: {e}")
            time.sleep(5)  # 오류 발생 시 대기 후 재시도

        finally:
            # 연결 유지 확인
            if not conn.open:
                conn = connect_to_rds()
                cursor = conn.cursor()

    cursor.close()
    conn.close()

if __name__ == "__main__":
    process_rds()
