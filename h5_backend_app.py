"""
复盘宝 - Flask后端
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
import urllib.parse
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
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT UNIQUE,
            wechat_openid TEXT UNIQUE,
            nickname TEXT,
            avatar TEXT,
            vip_expiry TIMESTAMP,
            invite_code TEXT UNIQUE,
            auto_token TEXT,
            auto_token_expiry TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS sms_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            code TEXT NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            used INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
        # 对齐H5前端字段名：value/changePercent/type
        return {
            'name': name,
            'code': code,
            'value': round(price, 2),       # 前端期望 value
            'change': round(change, 2),
            'changePercent': round(pct, 2),  # 前端期望 changePercent
            'type': 'up' if change > 0 else 'down' if change < 0 else 'flat',  # 前端期望 type
            'update_time': datetime.datetime.now().strftime('%H:%M:%S')
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
    # refresh=true 绕过缓存
    if request.args.get('refresh') != 'true':
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
        {'name': '上证指数', 'code': '000001', 'value': 3388.50 + (now.second % 10), 'change': 25.67, 'changePercent': 0.76, 'type': 'up', 'update_time': now.strftime('%H:%M:%S')},
        {'name': '深证成指', 'code': '399001', 'value': 10856.30, 'change': -42.15, 'changePercent': -0.39, 'type': 'down', 'update_time': now.strftime('%H:%M:%S')},
        {'name': '创业板指', 'code': '399006', 'value': 2234.80, 'change': 18.92, 'changePercent': 0.85, 'type': 'up', 'update_time': now.strftime('%H:%M:%S')},
        {'name': '沪深300', 'code': '000300', 'value': 3956.20, 'change': 12.45, 'changePercent': 0.32, 'type': 'up', 'update_time': now.strftime('%H:%M:%S')},
    ]


@app.route('/api/ztlist', methods=['GET'])
def get_ztlist():
    if request.args.get('refresh') != 'true':
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
    if request.args.get('refresh') != 'true':
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
    """获取个股详情 - 腾讯行情API"""
    try:
        market = 'sh' if code.startswith('6') else 'sz'
        resp = requests.get(f'{TENCENT_BASE}{market}{code}', headers=HEADERS, timeout=10)
        # 腾讯单股返回格式: v_sz000725="51~名称~代码~当前价~昨收~今开~..."
        text = resp.text
        if 'v_' not in text:
            return jsonify({'code': 1, 'msg': '无数据'}), 404
        # 取 = 后面的内容并分割
        inner = text.split('=', 1)[1].strip().strip('"')
        parts = inner.split('~')
        if len(parts) < 35:
            return jsonify({'code': 1, 'msg': '数据格式异常'}), 404
        # 腾讯字段映射（已验证，针对000725）：
        # parts[3]=当前价 parts[4]=昨收 parts[5]=今开
        # parts[31]=涨跌额 parts[32]=涨跌幅% parts[33]=日高(压力价) parts[34]=日低(支撑价)
        # parts[36]=成交量(股) parts[37]=成交额(万元) parts[38]=换手率% parts[39]=换手率2?
        # parts[43]=振幅% parts[44]=总市值亿
        current_price = safe_float(parts[3])
        yesterday_close = safe_float(parts[4])
        pct = safe_float(parts[32])  # 已经是%
        turnover_wan = safe_float(parts[37])  # 万元
        volume_shares = safe_float(parts[36])  # 股
        return jsonify({'code': 0, 'data': {
            'name': safe_str(parts[1]),
            'code': code,
            'price': current_price,
            'change': round(safe_float(parts[31]), 2),  # 涨跌额
            'pct': round(pct, 2),
            'open': safe_float(parts[5]),
            'high': safe_float(parts[33]),
            'low': safe_float(parts[34]),
            'volume': round(volume_shares / 10000 / 100, 2),  # 亿股
            'turnover': round(turnover_wan / 10000, 2),  # 亿元
            'amplitude': round(safe_float(parts[43]), 2),
            'turnover_rate': round(safe_float(parts[38]), 2),
            'market_cap': round(safe_float(parts[44]), 2),  # 亿元
            'sector': None,
        }})
    except Exception as e:
        return jsonify({'code': 1, 'msg': f'查询失败: {str(e)}'}), 500


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


@app.route('/admin', methods=['GET'])
def admin_page():
    """管理后台页面"""
    html = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>复盘宝 - 管理后台</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'PingFang SC', 'Microsoft YaHei', sans-serif;
            background: #0a0e17;
            color: #fff;
            min-height: 100vh;
            padding: 2rem;
        }
        .container { max-width: 900px; margin: 0 auto; }
        h1 { font-size: 1.5rem; margin-bottom: 1.5rem; color: #ffd700; }
        h2 { font-size: 1.1rem; color: #94a3b8; margin: 1.5rem 0 0.75rem; }
        .stats-row { display: flex; gap: 1rem; flex-wrap: wrap; margin-bottom: 2rem; }
        .stat-card {
            background: #111827;
            border: 1px solid #2a3548;
            border-radius: 12px;
            padding: 1.25rem;
            flex: 1;
            min-width: 150px;
        }
        .stat-label { font-size: 0.75rem; color: #64748b; margin-bottom: 0.5rem; }
        .stat-value { font-size: 1.75rem; font-weight: 700; color: #ffd700; }
        .stat-value.green { color: #00d084; }
        .search-box { display: flex; gap: 0.5rem; margin-bottom: 1.5rem; }
        .search-box input {
            flex: 1;
            padding: 0.75rem 1rem;
            background: #111827;
            border: 1px solid #2a3548;
            border-radius: 8px;
            color: #fff;
            font-size: 0.9rem;
        }
        .search-box input:focus { outline: none; border-color: #ffd700; }
        .search-box button {
            padding: 0.75rem 1.5rem;
            background: linear-gradient(135deg, #ffd700, #f59e0b);
            color: #000;
            border: none;
            border-radius: 8px;
            font-weight: 700;
            cursor: pointer;
        }
        table { width: 100%; border-collapse: collapse; }
        th {
            text-align: left;
            padding: 0.75rem 1rem;
            background: #111827;
            color: #64748b;
            font-size: 0.75rem;
            font-weight: 600;
            border-bottom: 1px solid #2a3548;
        }
        td {
            padding: 0.875rem 1rem;
            border-bottom: 1px solid #1a2035;
            font-size: 0.85rem;
        }
        tr:hover td { background: #111827; }
        .rank-medal { font-size: 1.2rem; }
        .level-badge {
            display: inline-block;
            padding: 0.2rem 0.6rem;
            border-radius: 20px;
            font-size: 0.7rem;
            font-weight: 700;
        }
        .level-diamond { background: rgba(185, 242, 255, 0.15); color: #b9f2ff; }
        .level-gold { background: rgba(255, 215, 0, 0.15); color: #ffd700; }
        .level-bronze { background: rgba(205, 127, 50, 0.15); color: #cd7f32; }
        .user-detail {
            background: #111827;
            border: 1px solid #2a3548;
            border-radius: 12px;
            padding: 1.25rem;
            margin-bottom: 1.5rem;
        }
        .user-detail h3 { color: #ffd700; margin-bottom: 0.75rem; }
        .detail-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 1rem; }
        .detail-item label { font-size: 0.7rem; color: #64748b; display: block; }
        .detail-item span { font-size: 1rem; font-weight: 600; color: #fff; }
        .refresh-btn {
            padding: 0.5rem 1rem;
            background: #1a2035;
            border: 1px solid #2a3548;
            border-radius: 6px;
            color: #94a3b8;
            cursor: pointer;
            font-size: 0.8rem;
            margin-bottom: 1rem;
        }
        .refresh-btn:hover { border-color: #ffd700; color: #ffd700; }
        .api-link { font-size: 0.75rem; color: #64748b; margin-top: 2rem; word-break: break-all; }
        .msg { color: #00d084; font-size: 0.85rem; margin-top: 0.5rem; }
        .msg.error { color: #ff4757; }
    </style>
</head>
<body>
    <div class="container">
        <h1>📊 复盘宝 - 邀请管理后台</h1>
        <div class="stats-row">
            <div class="stat-card"><div class="stat-label">总邀请人数</div><div class="stat-value" id="totalInvites">-</div></div>
            <div class="stat-card"><div class="stat-label">活跃邀请码</div><div class="stat-value green" id="activeCodes">-</div></div>
            <div class="stat-card"><div class="stat-label">累计VIP天数</div><div class="stat-value" id="totalVipDays">-</div></div>
        </div>
        <h2>🔍 查询邀请码</h2>
        <div class="search-box">
            <input type="text" id="searchInput" placeholder="输入邀请码，如 QM2026ABCD" />
            <button onclick="searchCode()">查询</button>
        </div>
        <div id="searchResult"></div>
        <h2>🏆 邀请排行榜</h2>
        <button class="refresh-btn" onclick="loadLeaderboard()">🔄 刷新</button>
        <table>
            <thead><tr><th>排名</th><th>昵称</th><th>邀请码</th><th>等级</th><th>邀请人数</th><th>VIP天数</th></tr></thead>
            <tbody id="leaderboardBody"><tr><td colspan="6" style="text-align:center;color:#64748b;padding:2rem;">加载中...</td></tr></tbody>
        </table>
        <div class="api-link">
            API: GET /api/invite/leaderboard | GET /api/invite/stats?code=XXX | POST /api/invite/record<br>
            后端：https://h5-backend-tgoe.onrender.com | 管理后台：https://h5-backend-tgoe.onrender.com/admin
        </div>
    </div>
    <script>
        const API = '';
        function loadLeaderboard() {
            fetch('/api/invite/leaderboard').then(r => r.json()).then(data => {
                if (data.code !== 0) return;
                const leaders = data.data;
                let totalInvites = 0, totalVipDays = 0;
                leaders.forEach(l => { totalInvites += l.total_invites; totalVipDays += l.total_vip_days; });
                document.getElementById('totalInvites').textContent = totalInvites;
                document.getElementById('activeCodes').textContent = leaders.length;
                document.getElementById('totalVipDays').textContent = totalVipDays;
                document.getElementById('leaderboardBody').innerHTML = leaders.map((item, i) => `
                    <tr><td><span class="rank-medal">${item.medal || (i+1)}</span></td>
                    <td>${item.owner_name}</td>
                    <td><code style="color:#94a3b8;font-size:0.8rem">${item.code}</code></td>
                    <td><span class="level-badge level-${item.level}">${item.level === 'diamond' ? '💎钻石' : item.level === 'gold' ? '🥇黄金' : '🥉青铜'}</span></td>
                    <td><b style="color:#00d084">${item.total_invites}</b> 人</td>
                    <td>${item.total_vip_days} 天</td></tr>
                `).join('');
            }).catch(() => {});
        }
        function searchCode() {
            const code = document.getElementById('searchInput').value.trim().toUpperCase();
            if (!code) return;
            const resultDiv = document.getElementById('searchResult');
            resultDiv.innerHTML = '<p style="color:#64748b">查询中...</p>';
            fetch('/api/invite/stats?code=' + code).then(r => r.json()).then(data => {
                if (data.code === 1) { resultDiv.innerHTML = '<p class="msg error">邀请码不存在</p>'; return; }
                const d = data.data;
                resultDiv.innerHTML = '<div class="user-detail"><h3>👤 ' + d.owner_name + '</h3><div class="detail-grid"><div class="detail-item"><label>邀请码</label><span>' + d.code + '</span></div><div class="detail-item"><label>等级</label><span class="level-badge level-' + d.level + '">' + (d.level === 'diamond' ? '💎钻石' : d.level === 'gold' ? '🥇黄金' : '🥉青铜') + '</span></div><div class="detail-item"><label>累计邀请</label><span style="color:#00d084;font-size:1.3rem">' + d.total_invites + '</span> 人</div><div class="detail-item"><label>累计VIP</label><span>' + d.total_vip_days + ' 天</span></div></div><p class="msg">注册时间：' + (d.created_at ? d.created_at.split(' ')[0] : '未知') + '</p></div>';
            }).catch(() => { resultDiv.innerHTML = '<p class="msg error">查询失败</p>'; });
        }
        loadLeaderboard();
    </script>
</body>
</html>
    '''
    return html


# ============ 登录认证 API ============

import hashlib
import secrets

# 微信OAuth配置（硬编码，演示用）
WECHAT_APP_ID = 'wxfdefe2ad09eb5af7'
WECHAT_APP_SECRET = 'c212f6ced284d62de00615bb631a1797'


def generate_user_session(user_id):
    """生成简单会话token"""
    token = secrets.token_hex(16)
    # 简单起见，用dict模拟会话存储（生产环境用Redis）
    if not hasattr(app, '_sessions'):
        app._sessions = {}
    app._sessions[token] = {
        'user_id': user_id,
        'expires': datetime.datetime.now() + datetime.timedelta(days=7)
    }
    return token


def verify_session(token):
    """验证会话token"""
    if not hasattr(app, '_sessions'):
        return None
    session = app._sessions.get(token)
    if not session:
        return None
    if session['expires'] < datetime.datetime.now():
        del app._sessions[token]
        return None
    return session['user_id']


def get_user_from_db(user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT id, phone, nickname, avatar, vip_expiry, invite_code, created_at FROM users WHERE id = ?', (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {
            'id': row[0], 'phone': row[1], 'nickname': row[2],
            'avatar': row[3], 'vip_expiry': row[4],
            'invite_code': row[5], 'created_at': row[6]
        }
    return None


def make_code():
    """生成6位验证码"""
    return str(random.randint(100000, 999999))


def do_send_sms(phone, code):
    """通过短信宝发送真实短信"""
    import hashlib
    import urllib.request

    SMSBAO_USERNAME = 'kenli'
    SMSBAO_API_KEY = 'ef5d6a1e7c9a4beba829bbf01dc247f7'
    SMS_SEND_URL = 'http://api.smsbao.com/sms'

    try:
        # 密码 = md5(api_key)
        pwd = hashlib.md5(SMSBAO_API_KEY.encode()).hexdigest()

        params = urllib.parse.urlencode({
            'u': SMSBAO_USERNAME,
            'p': pwd,
            'm': phone,
            'c': f'【广西晶晶亮新能源科技有限公司】您的验证码是{code}。如非本人操作，请忽略本短信'
        })
        req = urllib.request.Request(f"{SMS_SEND_URL}?{params}")
        resp = urllib.request.urlopen(req, timeout=10)
        result = resp.read().decode()

        # 0=成功, 30=密码错误, 41=余额不足, 42=账号异常, 43=黑名单
        if result == '0':
            return jsonify({'code': 0, 'msg': '发送成功', 'debug_code': code})
        else:
            error_map = {
                '30': '短信宝账号密码错误',
                '41': '短信宝余额不足（请充值）',
                '42': '账号异常',
                '43': '号码在黑名单中'
            }
            return jsonify({'code': 1, 'msg': error_map.get(result, f'短信发送失败[{result}]')})
    except Exception as e:
        return jsonify({'code': 1, 'msg': f'短信服务异常: {str(e)}'})


@app.route('/api/auth/send_code', methods=['POST'])
def send_sms_code():
    """发送短信验证码"""
    data = request.get_json() or {}
    phone = data.get('phone', '').strip()

    # 验证手机号格式
    if not phone or len(phone) != 11 or not phone.startswith('1'):
        return jsonify({'code': 1, 'msg': '手机号格式错误'}), 400

    code = make_code()
    expires_at = datetime.datetime.now() + datetime.timedelta(minutes=5)

    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE sms_codes SET used = 1 WHERE phone = ?', (phone,))
    c.execute('INSERT INTO sms_codes (phone, code, expires_at) VALUES (?, ?, ?)',
              (phone, code, expires_at))
    conn.commit()
    conn.close()

    # 实际发送短信（短信宝）
    return do_send_sms(phone, code)


@app.route('/api/auth/login', methods=['POST'])
def login_with_code():
    """验证码登录/注册"""
    data = request.get_json() or {}
    phone = data.get('phone', '').strip()
    code = data.get('code', '').strip()

    if not phone or not code:
        return jsonify({'code': 1, 'msg': '手机号和验证码不能为空'}), 400

    conn = get_db()
    c = conn.cursor()

    # 验证验证码
    c.execute('''
        SELECT id FROM sms_codes
        WHERE phone = ? AND code = ? AND used = 0 AND expires_at > ?
        ORDER BY id DESC LIMIT 1
    ''', (phone, code, datetime.datetime.now()))
    row = c.fetchone()

    if not row:
        conn.close()
        return jsonify({'code': 1, 'msg': '验证码错误或已过期'}), 400

    # 标记验证码已使用
    c.execute('UPDATE sms_codes SET used = 1 WHERE phone = ? AND code = ?', (phone, code))

    # 查找或创建用户
    c.execute('SELECT id FROM users WHERE phone = ?', (phone,))
    user_row = c.fetchone()

    if user_row:
        user_id = user_row[0]
    else:
        # 新用户：创建账户
        nickname = f'用户{phone[-4:]}'
        invite_code = f'QM{phone[-8:].upper()}'
        c.execute('''
            INSERT INTO users (phone, nickname, invite_code, vip_expiry)
            VALUES (?, ?, ?, ?)
        ''', (phone, nickname, invite_code,
              datetime.datetime.now() + datetime.timedelta(days=7)))
        user_id = c.lastrowid

    conn.commit()
    conn.close()

    # 生成会话
    token = generate_user_session(user_id)
    user = get_user_from_db(user_id)

    # 生成7天免登录token
    auto_token = secrets.token_hex(16)
    auto_expiry = datetime.datetime.now() + datetime.timedelta(days=7)

    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE users SET auto_token = ?, auto_token_expiry = ? WHERE id = ?',
              (auto_token, auto_expiry, user_id))
    conn.commit()
    conn.close()

    return jsonify({'code': 0, 'data': {
        'token': token,
        'auto_token': auto_token,
        'user': user
    }})


@app.route('/api/auth/wechat/qr', methods=['GET'])
def wechat_qr():
    """获取微信登录状态轮询URL（扫码登录）"""
    if not WECHAT_APP_ID:
        return jsonify({
            'code': 0,
            'data': {
                'qr_url': None,
                'mock': True,
                'msg': '微信登录需要在环境变量配置 WECHAT_APP_ID 和 WECHAT_APP_SECRET'
            }
        })

    # 生成state参数用于标识本次登录会话
    state = secrets.token_hex(16)
    if not hasattr(app, '_wechat_states'):
        app._wechat_states = {}
    app._wechat_states[state] = {'created': datetime.datetime.now(), 'authenticated': False, 'token': None, 'user': None}

    redirect_uri = 'https://h5-backend-tgoe.onrender.com/api/auth/wechat/callback'
    auth_url = (
        f'https://open.weixin.qq.com/connect/oauth2/authorize'
        f'?appid={WECHAT_APP_ID}'
        f'&redirect_uri={urllib.parse.quote(redirect_uri)}'
        f'&response_type=code&scope=snsapi_userinfo'
        f'&state={state}#wechat_redirect'
    )

    return jsonify({
        'code': 0,
        'data': {
            'state': state,
            'auth_url': auth_url,
            'poll_url': f'https://h5-backend-tgoe.onrender.com/api/auth/wechat/poll/{state}'
        }
    })


@app.route('/api/auth/wechat/poll/<state>', methods=['GET'])
def wechat_login_poll(state):
    """轮询微信登录状态"""
    if not hasattr(app, '_wechat_states') or state not in app._wechat_states:
        return jsonify({'code': 1, 'msg': 'state无效'})

    entry = app._wechat_states[state]
    if not entry['authenticated']:
        return jsonify({'code': 0, 'data': {'ready': False}})

    return jsonify({
        'code': 0,
        'data': {
            'ready': True,
            'token': entry['token'],
            'user': entry['user']
        }
    })


@app.route('/api/auth/wechat/callback', methods=['GET'])
def wechat_callback():
    """微信OAuth回调"""
    code = request.args.get('code', '')
    state = request.args.get('state', '')
    errcode = request.args.get('errcode', '')

    if errcode:
        return jsonify({'code': 1, 'msg': f'微信授权失败: {errcode}'}), 400

    if not hasattr(app, '_wechat_states') or state not in app._wechat_states:
        return jsonify({'code': 1, 'msg': 'state无效或已过期'}), 400

    entry = app._wechat_states[state]

    if not WECHAT_APP_ID or not WECHAT_APP_SECRET:
        return jsonify({'code': 1, 'msg': '未配置微信AppID'}), 400

    # 换取access_token和openid
    token_url = (
        f'https://api.weixin.qq.com/sns/oauth2/access_token'
        f'?appid={WECHAT_APP_ID}&secret={WECHAT_APP_SECRET}&code={code}&grant_type=authorization_code'
    )
    try:
        resp = requests.get(token_url, timeout=10)
        token_data = resp.json()
        openid = token_data.get('openid')
        access_token = token_data.get('access_token')
    except Exception:
        return jsonify({'code': 1, 'msg': '微信服务器通信失败'}), 500

    if not openid:
        return jsonify({'code': 1, 'msg': '获取openid失败'}), 500

    # 获取用户信息
    user_info_url = f'https://api.weixin.qq.com/sns/userinfo?access_token={access_token}&openid={openid}'
    try:
        resp2 = requests.get(user_info_url, timeout=10)
        wx_user = resp2.json()
        nickname = wx_user.get('nickname', '微信用户')
        avatar = wx_user.get('headimgurl', '')
    except Exception:
        nickname = '微信用户'
        avatar = ''

    # 查找或创建用户
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT id FROM users WHERE wechat_openid = ?', (openid,))
    user_row = c.fetchone()

    if user_row:
        user_id = user_row[0]
    else:
        invite_code = f'WX{openid[-8:].upper()}'
        c.execute('''
            INSERT INTO users (wechat_openid, nickname, avatar, invite_code, vip_expiry)
            VALUES (?, ?, ?, ?, ?)
        ''', (openid, nickname, avatar, invite_code,
              datetime.datetime.now() + datetime.timedelta(days=7)))
        user_id = c.lastrowid

    conn.commit()
    conn.close()

    token = generate_user_session(user_id)
    user = get_user_from_db(user_id)

    # 标记认证完成，H5轮询会拿到结果
    entry['authenticated'] = True
    entry['token'] = token
    entry['user'] = user

    # 返回成功页面（回调会显示这个）
    html = f'''
    <!DOCTYPE html>
    <html>
    <body>
    <div style="text-align:center;padding-top:50px;font-family:sans-serif;">
        <h2 style="color:#52c41a;">✅ 授权成功</h2>
        <p>可以关闭此页面了</p>
        <script>
        if (window.opener) {{
            window.opener.postMessage({{token: '{token}', user_id: '{user_id}'}}, '*');
        }}
        setTimeout(() => window.close(), 2000);
        </script>
    </div>
    </body>
    </html>
    '''
    return html


@app.route('/api/auth/status', methods=['GET'])
def auth_status():
    """检查登录状态"""
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    if not token:
        return jsonify({'code': 0, 'data': {'logged_in': False}})

    user_id = verify_session(token)
    if not user_id:
        return jsonify({'code': 0, 'data': {'logged_in': False}})

    user = get_user_from_db(user_id)
    if not user:
        return jsonify({'code': 0, 'data': {'logged_in': False}})

    return jsonify({'code': 0, 'data': {'logged_in': True, 'user': user}})


@app.route('/api/auth/auto_login', methods=['POST'])
def auto_login():
    """检测auto_token自动登录"""
    auto_token = request.headers.get('X-Auto-Token', '').strip()
    if not auto_token:
        return jsonify({'code': 1, 'msg': '缺少auto_token'})

    conn = get_db()
    c = conn.cursor()
    c.execute('''
        SELECT id FROM users
        WHERE auto_token = ? AND auto_token_expiry > ?
    ''', (auto_token, datetime.datetime.now()))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({'code': 1, 'msg': '登录状态已过期，请重新验证'})

    user_id = row[0]
    conn.close()

    token = generate_user_session(user_id)
    user = get_user_from_db(user_id)

    return jsonify({'code': 0, 'data': {'token': token, 'user': user}})


@app.route('/api/auth/logout', methods=['POST'])
def logout():
    """退出登录：清除auto_token"""
    auto_token = request.headers.get('X-Auto-Token', '').strip()
    if auto_token:
        conn = get_db()
        c = conn.cursor()
        c.execute('UPDATE users SET auto_token = NULL, auto_token_expiry = NULL WHERE auto_token = ?',
                  (auto_token,))
        conn.commit()
        conn.close()

    return jsonify({'code': 0, 'msg': '已退出登录'})


if __name__ == '__main__':
    print('复盘宝后端启动')
    app.run(host='0.0.0.0', port=5000)
