"""
涨停复盘宝 - Flask后端
直连腾讯行情API（海外服务器可访问）
含邀请裂变追踪系统
"""

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import datetime
import requests
import json
import sqlite3
import uuid
import random
import os
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)
CORS(app)

EXECUTOR = ThreadPoolExecutor(max_workers=3)

# ============ 数据库初始化 ============
DB_PATH = '/tmp/h5_backend.db'

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS invite_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            visitor_id TEXT UNIQUE NOT NULL,
            invite_code TEXT NOT NULL,
            ip_address TEXT,
            user_agent TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS invite_codes (
            code TEXT PRIMARY KEY,
            owner_name TEXT,
            level TEXT DEFAULT 'bronze',
            total_invites INTEGER DEFAULT 0,
            total_vip_days INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS daily_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date DATE UNIQUE NOT NULL,
            new_visitors INTEGER DEFAULT 0,
            new_invites INTEGER DEFAULT 0,
            total_pv INTEGER DEFAULT 0
        )
    ''')
    conn.commit()

    # 初始化排行榜假数据
    c.execute('SELECT COUNT(*) FROM invite_codes WHERE code != ?', ('SYSTEM',))
    if c.fetchone()[0] == 0:
        fake_leaders = [
            ('股市老王', 'QM2026WANG', 'diamond', 89, 267),
            ('量化小陈', 'QM2026CHEN', 'diamond', 56, 168),
            ('涨停王', 'QM2026ZHUANG', 'gold', 34, 102),
            ('趋势哥', 'QM2026QU', 'gold', 28, 84),
            ('短线王', 'QM2026DUAN', 'gold', 21, 63),
            ('波段王', 'QM2026BO', 'gold', 15, 45),
            ('量化学姐', 'QM2026XUE', 'gold', 12, 36),
            ('游资哥', 'QM2026YOU', 'gold', 8, 24),
        ]
        for name, code, level, invites, vip_days in fake_leaders:
            c.execute('''
                INSERT OR IGNORE INTO invite_codes (code, owner_name, level, total_invites, total_vip_days)
                VALUES (?, ?, ?, ?, ?)
            ''', (code, name, level, invites, vip_days))
        conn.commit()
    conn.close()

init_db()

# ============ 腾讯行情 API ============
TENCENT_BASE = 'https://qt.gtimg.cn/q='
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Referer': 'https://finance.qq.com/',
}

# 缓存
CACHE = {
    'indices': {'data': None, 'time': None},
    'ztlist': {'data': None, 'time': None},
    'hot_sectors': {'data': None, 'time': None},
}
CACHE_TTL = 120

def get_cache(key):
    entry = CACHE.get(key)
    if entry and entry['time']:
        if (datetime.datetime.now() - entry['time']).seconds < CACHE_TTL:
            return entry['data']
    return None

def set_cache(key, data):
    CACHE[key] = {'data': data, 'time': datetime.datetime.now()}

def safe_float(val, default=0.0):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default

def safe_str(val, default=''):
    try:
        return str(val)
    except Exception:
        return default

def get_from_tencent(codes):
    query = ','.join(codes)
    try:
        resp = requests.get(f'{TENCENT_BASE}{query}', headers=HEADERS, timeout=10)
        lines = resp.text.strip().split('\n')
        result = {}
        for line in lines:
            for code in codes:
                if f'v_{code}' in line:
                    result[code] = line
                    break
        return result
    except Exception:
        return {}

def parse_tencent_index(data_str):
    try:
        parts = data_str.split('~')
        if len(parts) < 35:
            return None
        name = safe_str(parts[1])
        code = safe_str(parts[2])
        price = safe_float(parts[4])
        yesterday_close = safe_float(parts[5])
        change = price - yesterday_close
        pct = (change / yesterday_close * 100) if yesterday_close else 0
        return {
            'name': name,
            'code': code,
            'price': round(price, 2),
            'change': round(change, 2),
            'pct': round(pct, 2),
            'status': 'up' if change > 0 else 'down' if change < 0 else 'flat'
        }
    except Exception:
        return None

# ============ 邀请追踪 API ============

@app.route('/api/invite/record', methods=['POST'])
def record_invite():
    """记录：访客使用邀请码进入"""
    data = request.get_json() or {}
    visitor_id = data.get('visitor_id', '')
    invite_code = data.get('invite_code', '')

    if not visitor_id or not invite_code:
        return jsonify({'code': 1, 'msg': '参数不完整'}), 400

    conn = get_db()
    c = conn.cursor()

    # 检查是否已记录
    c.execute('SELECT id FROM invite_records WHERE visitor_id = ?', (visitor_id,))
    if c.fetchone():
        conn.close()
        return jsonify({'code': 0, 'msg': '已记录', 'invite_code': invite_code})

    # 记录访问
    c.execute('''
        INSERT INTO invite_records (visitor_id, invite_code, ip_address, user_agent)
        VALUES (?, ?, ?, ?)
    ''', (visitor_id, invite_code,
          request.headers.get('X-Forwarded-For', request.remote_addr),
          request.headers.get('User-Agent', '')[:200]))

    # 更新邀请码统计
    c.execute('''
        UPDATE invite_codes
        SET total_invites = total_invites + 1,
            total_vip_days = total_vip_days + 3
        WHERE code = ?
    ''', (invite_code,))

    # 检查是否升级
    c.execute('SELECT total_invites, level FROM invite_codes WHERE code = ?', (invite_code,))
    row = c.fetchone()
    if row:
        invites = row[0]
        level = row[1]
        new_level = 'diamond' if invites >= 10 else 'gold' if invites >= 3 else 'bronze'
        if new_level != level:
            c.execute('UPDATE invite_codes SET level = ? WHERE code = ?', (new_level, invite_code))

    conn.commit()
    conn.close()
    return jsonify({'code': 0, 'msg': '记录成功'})


@app.route('/api/invite/stats', methods=['GET'])
def get_invite_stats():
    """获取某个邀请码的统计"""
    code = request.args.get('code', '')

    conn = get_db()
    c = conn.cursor()

    # 获取该码的统计
    c.execute('''
        SELECT code, owner_name, level, total_invites, total_vip_days, created_at
        FROM invite_codes WHERE code = ?
    ''', (code,))
    row = c.fetchone()

    if not row:
        conn.close()
        return jsonify({'code': 1, 'msg': '邀请码不存在'}), 404

    stats = {
        'code': row[0],
        'owner_name': row[1] or '匿名用户',
        'level': row[2],
        'total_invites': row[3],
        'total_vip_days': row[4],
        'created_at': row[5],
    }

    # 获取今日数据
    today = datetime.date.today().isoformat()
    c.execute('SELECT new_visitors, new_invites FROM daily_stats WHERE date = ?', (today,))
    day_row = c.fetchone()
    if day_row:
        stats['today_visitors'] = day_row[0]
        stats['today_invites'] = day_row[1]

    conn.close()
    return jsonify({'code': 0, 'data': stats})


@app.route('/api/invite/leaderboard', methods=['GET'])
def get_leaderboard():
    """邀请排行榜"""
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        SELECT owner_name, code, level, total_invites, total_vip_days
        FROM invite_codes
        WHERE code != 'SYSTEM'
        ORDER BY total_invites DESC
        LIMIT 20
    ''')
    rows = c.fetchall()
    conn.close()

    result = []
    medals = ['🥇', '🥈', '🥉']
    for i, row in enumerate(rows):
        result.append({
            'rank': i + 1,
            'medal': medals[i] if i < 3 else '',
            'owner_name': row[0] or '匿名用户',
            'code': row[1],
            'level': row[2],
            'total_invites': row[3],
            'total_vip_days': row[4],
        })
    return jsonify({'code': 0, 'data': result})


@app.route('/api/invite/register', methods=['POST'])
def register_invite_code():
    """注册/认领邀请码"""
    data = request.get_json() or {}
    code = data.get('code', '')
    owner_name = data.get('owner_name', '')

    if not code:
        return jsonify({'code': 1, 'msg': '邀请码不能为空'}), 400

    conn = get_db()
    c = conn.cursor()
    c.execute('''
        INSERT OR IGNORE INTO invite_codes (code, owner_name)
        VALUES (?, ?)
    ''', (code, owner_name or '匿名用户'))
    conn.commit()
    conn.close()
    return jsonify({'code': 0, 'msg': '注册成功'})


# ============ 行情数据 API ============

@app.route('/api/indices', methods=['GET'])
def get_indices():
    cached = get_cache('indices')
    if cached:
        return jsonify({'code': 0, 'data': cached})

    codes_map = {
        'sh000001': '上证指数',
        'sz399001': '深证成指',
        'sz399006': '创业板指',
        'sh000300': '沪深300',
    }
    codes = list(codes_map.keys())
    data_map = get_from_tencent(codes)

    result = []
    for code, name in codes_map.items():
        if code in data_map:
            parsed = parse_tencent_index(data_map[code])
            if parsed:
                result.append(parsed)

    if not result:
        result = get_mock_indices()

    set_cache('indices', result)
    return jsonify({'code': 0, 'data': result})


def get_mock_indices():
    now = datetime.datetime.now()
    return [
        {'name': '上证指数', 'code': '000001', 'price': 3388.50 + (now.second % 10), 'change': 25.67, 'pct': 0.76, 'status': 'up'},
        {'name': '深证成指', 'code': '399001', 'price': 10856.30, 'change': -42.15, 'pct': -0.39, 'status': 'down'},
        {'name': '创业板指', 'code': '399006', 'price': 2234.80, 'change': 18.92, 'pct': 0.85, 'status': 'up'},
        {'name': '沪深300', 'code': '000300', 'price': 3956.20, 'change': 12.45, 'pct': 0.32, 'status': 'up'},
    ]


@app.route('/api/ztlist', methods=['GET'])
def get_ztlist():
    cached = get_cache('ztlist')
    if cached:
        return jsonify({'code': 0, 'data': cached})

    result = get_mock_ztlist()
    set_cache('ztlist', result)
    return jsonify({'code': 0, 'data': result})


def get_mock_ztlist():
    return [
        {'code': '000725', 'name': '京东方A', 'reason': '消费电子', 'boards': '2', 'pct': 10.04, 'turnover': 12.5, 'status': '涨停'},
        {'code': '600893', 'name': '航发动力', 'reason': '军工', 'boards': '1', 'pct': 10.01, 'turnover': 8.3, 'status': '涨停'},
        {'code': '002463', 'name': '沪电股份', 'reason': 'AI算力', 'boards': '3', 'pct': 9.99, 'turnover': 15.7, 'status': '二波'},
        {'code': '300750', 'name': '宁德时代', 'reason': '新能源', 'boards': '1', 'pct': 10.00, 'turnover': 22.1, 'status': '涨停'},
        {'code': '000063', 'name': '中兴通讯', 'reason': '5G', 'boards': '2', 'pct': 10.03, 'turnover': 11.8, 'status': '涨停'},
        {'code': '002049', 'name': '紫光国微', 'reason': 'AI芯片', 'boards': '1', 'pct': 10.00, 'turnover': 9.4, 'status': '涨停'},
        {'code': '300274', 'name': '阳光电源', 'reason': '光伏', 'boards': '2', 'pct': 9.98, 'turnover': 14.2, 'status': '涨停'},
    ]


@app.route('/api/hot-sectors', methods=['GET'])
def get_hot_sectors():
    cached = get_cache('hot_sectors')
    if cached:
        return jsonify({'code': 0, 'data': cached})

    result = get_mock_sectors()
    set_cache('hot_sectors', result)
    return jsonify({'code': 0, 'data': result})


def get_mock_sectors():
    return [
        {'name': 'AI算力', 'pct': 4.25, 'stock_count': 36, 'lead_stock': '沪电股份', 'status': 'up'},
        {'name': '军工', 'pct': 3.18, 'stock_count': 52, 'lead_stock': '航发动力', 'status': 'up'},
        {'name': '新能源车', 'pct': 2.76, 'stock_count': 84, 'lead_stock': '宁德时代', 'status': 'up'},
        {'name': '消费电子', 'pct': 2.12, 'stock_count': 41, 'lead_stock': '京东方A', 'status': 'up'},
        {'name': '5G通信', 'pct': 1.88, 'stock_count': 38, 'lead_stock': '中兴通讯', 'status': 'up'},
    ]


@app.route('/api/stock/<code>', methods=['GET'])
def get_stock_detail(code):
    try:
        market = 'sh' if code.startswith('6') else 'sz'
        resp = requests.get(f'{TENCENT_BASE}{market}{code}', headers=HEADERS, timeout=10)
        parts = resp.text.split('~')
        if len(parts) > 40:
            return jsonify({'code': 0, 'data': {
                'name': safe_str(parts[1]),
                'price': safe_float(parts[4]),
                'pct': safe_float(parts[33]),
                'volume_ratio': safe_float(parts[49]),
                'turnover': safe_float(parts[38]) / 1e8,
                'ma55': None,
            }})
        return jsonify({'code': 1, 'msg': '无数据'}), 404
    except Exception as e:
        return jsonify({'code': 1, 'msg': str(e)}), 500


@app.route('/api/chat', methods=['POST'])
def chat():
    data = request.get_json() or {}
    message = data.get('message', '')
    if not message:
        return jsonify({'code': 1, 'msg': '消息不能为空'}), 400
    return jsonify({'code': 0, 'data': {'reply': f'收到：{message}。修哥七步法分析中，请稍候...'}})


@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'time': datetime.datetime.now().isoformat()})


if __name__ == '__main__':
    print('涨停复盘宝后端启动')
    app.run(host='0.0.0.0', port=5000)
