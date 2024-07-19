import os
import sqlite3
from flask import Flask, request, send_from_directory, jsonify, render_template, g, make_response, Response
import ffmpeg
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix
from threading import Thread
import time
import mimetypes
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
cors = CORS(app, resources={r"*": {"origins": "*"}})

VIDEO_DIRS = [os.getenv('VIDEO_DIR')]
THUMBNAIL_DIR = os.getenv('THUMBNAIL_DIR')
DATABASE = os.getenv('DATABASE')

def get_db():
    """데이터베이스 연결을 가져옵니다. 존재하지 않으면 새로 연결을 만듭니다."""
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE, timeout=10)  # timeout 설정
    return db

@app.teardown_appcontext
def close_connection(exception):
    """요청이 끝나면 데이터베이스 연결을 닫습니다."""
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

# 썸네일 디렉토리가 존재하지 않으면 생성
if not os.path.exists(THUMBNAIL_DIR):
    os.makedirs(THUMBNAIL_DIR)

# 데이터베이스 초기화
def init_db():
    with app.app_context():
        db = get_db()
        cursor = db.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY,
                filename TEXT UNIQUE,
                views INTEGER DEFAULT 0
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS search_history (
                id INTEGER PRIMARY KEY,
                query TEXT UNIQUE
            )
        ''')
        db.commit()

def create_thumbnail(video_path, thumbnail_path):
    """비디오 파일에서 썸네일을 생성"""
    try:
        (
            ffmpeg
            .input(video_path, ss=15)  # 첫 15초의 프레임을 썸네일로 사용
            .output(thumbnail_path, vframes=1, format='image2', vcodec='mjpeg')
            .run(capture_stdout=True, capture_stderr=True)
        )
    except ffmpeg.Error as e:
        print(f"Error creating thumbnail for {video_path}: {e.stderr.decode()}")

def generate_thumbnails(app):
    """모든 비디오 파일에 대한 썸네일을 미리 생성"""
    with app.app_context():
        for video_dir in VIDEO_DIRS:
            videos = [f for f in os.listdir(video_dir) if f.endswith('.mp4')]
            for video in videos:
                video_path = os.path.join(video_dir, video)
                thumbnail_filename = f"{os.path.splitext(video)[0]}.jpg"
                thumbnail_path = os.path.join(THUMBNAIL_DIR, thumbnail_filename)
                if not os.path.exists(thumbnail_path):
                    create_thumbnail(video_path, thumbnail_path)
                add_video_to_db(video)

def add_video_to_db(filename):
    """비디오 파일을 데이터베이스에 추가"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('''
        INSERT OR IGNORE INTO videos (filename)
        VALUES (?)
    ''', (filename,))
    db.commit()

def delete_video_from_db(filename):
    """데이터베이스에서 비디오 파일을 삭제"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('''
        DELETE FROM videos WHERE filename = ?
    ''', (filename,))
    db.commit()

def clean_db(app):
    """데이터베이스에서 존재하지 않는 비디오 파일을 정리"""
    with app.app_context():
        db = get_db()
        cursor = db.cursor()
        cursor.execute('SELECT filename FROM videos')
        videos = [row[0] for row in cursor.fetchall()]
        for video in videos:
            video_exists = any(os.path.exists(os.path.join(dir, video)) for dir in VIDEO_DIRS if dir is not None and video is not None)
            if not video_exists:
                delete_video_from_db(video)

# 서버 시작 시 썸네일 미리 생성 및 데이터베이스 초기화
init_db()
Thread(target=generate_thumbnails, args=(app,)).start()

# 주기적으로 데이터베이스 정리 작업 실행 (예: 매 시간마다)
def schedule_cleanup(app):
    while True:
        clean_db(app)
        time.sleep(3600)  # 1시간마다 실행

Thread(target=schedule_cleanup, args=(app,)).start()

def add_cache_control(response, max_age=3600):
    """응답에 Cache-Control 헤더를 추가"""
    response.headers['Cache-Control'] = f'public, max-age={max_age}'
    return response

def get_range(request):
    range_header = request.headers.get('Range', None)
    if not range_header:
        return None, None, None
    ranges = range_header.strip().split('=')[-1]
    if ',' in ranges:
        return None, None, None  # Multiple ranges not supported
    ranges = ranges.split('-')
    start = int(ranges[0]) if ranges[0] else None
    end = int(ranges[1]) if len(ranges) > 1 and ranges[1] else None
    return start, end, ranges[0]

@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')

@app.route('/play', methods=['GET'])
def play():
    return render_template('play.html')

@app.route('/videos', methods=['GET'])
def list_videos():
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT filename FROM videos')
    videos = [row[0] for row in cursor.fetchall()]
    return jsonify(videos)

@app.route('/videos/popular', methods=['GET'])
def list_popular_videos():
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT filename FROM videos ORDER BY views DESC LIMIT 10')
    videos = [row[0] for row in cursor.fetchall()]
    return jsonify(videos)

@app.route('/videos/<filename>', methods=['GET'])
def stream_video(filename):
    for video_dir in VIDEO_DIRS:
        video_path = os.path.join(video_dir, filename)
        if os.path.exists(video_path):
            start, end, range_header = get_range(request)
            file_size = os.path.getsize(video_path)
            if start is not None:
                start = min(start, file_size - 1)
            if end is None or end >= file_size:
                end = file_size - 1

            length = end - start + 1 if start is not None else file_size
            with open(video_path, 'rb') as video_file:
                if start is not None:
                    video_file.seek(start)
                data = video_file.read(length)

            response = Response(data, status=206 if start is not None else 200)
            response.headers.add('Content-Range', f'bytes {start}-{end}/{file_size}')
            response.headers.add('Accept-Ranges', 'bytes')
            response.headers.add('Content-Length', str(length))
            mime_type, _ = mimetypes.guess_type(video_path)
            response.headers.add('Content-Type', mime_type or 'application/octet-stream')
            return response

    return 'Video not found', 404

@app.route('/thumbnails/<filename>', methods=['GET'])
def get_thumbnail(filename):
    thumbnail_filename = f"{os.path.splitext(filename)[0]}.jpg"
    response = make_response(send_from_directory(THUMBNAIL_DIR, thumbnail_filename))
    return add_cache_control(response)

@app.route('/search', methods=['GET'])
def search_videos():
    query = request.args.get('q', '')
    is_complete = request.args.get('is_complete', 'false').lower() == 'true'
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT filename FROM videos WHERE filename LIKE ?', (f'%{query}%',))
    videos = [row[0] for row in cursor.fetchall()]

    if is_complete and query and videos:
        cursor.execute('INSERT OR IGNORE INTO search_history (query) VALUES (?)', (query,))
        cursor.execute('DELETE FROM search_history WHERE id NOT IN (SELECT id FROM search_history ORDER BY id DESC LIMIT 5)')
        db.commit()

    return jsonify(videos)

@app.route('/search/history', methods=['GET'])
def get_search_history():
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT query FROM search_history ORDER BY id DESC LIMIT 5')
    history = [row[0] for row in cursor.fetchall()]
    return jsonify(history)

@app.route('/video-info', methods=['GET'])
def video_info():
    video = request.args.get('video', '')
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT filename, views FROM videos WHERE filename = ?', (video,))
    row = cursor.fetchone()
    if row:
        video_info = {
            'title': row[0],
            'views': row[1]
        }
        return jsonify(video_info)
    return 'Video not found', 404

@app.route('/increment-views', methods=['POST'])
def increment_views():
    video = request.args.get('video', '')
    db = get_db()
    cursor = db.cursor()
    cursor.execute('UPDATE videos SET views = views + 1 WHERE filename = ?', (video,))
    db.commit()
    return '', 204

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
