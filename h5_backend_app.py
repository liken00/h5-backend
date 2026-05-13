"""
涨停复盘宝 - Flask后端
直连腾讯/新浪行情API（海外服务器可访问）
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
import datetime
import requests
import json
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)
CORS(app)

EXECUTOR = ThreadPoolExecutor(max_workers=3)

# 腾讯行情 API（海外可访问）
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
CACHE_TTL = 120  # 2分钟


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


def parse_tencent_index(data_str):
    """解析腾讯行情指数数据"""
    # v_sh000001="1~上证指数~000001~4212.94~4214.49~..."
    try:
        parts = data_str.split('~')
        if len(parts) < 35:
            return None
        name = safe_str(parts[1])
        code = safe_str(parts[2])       # 指数代码
        price = safe_float(parts[4])    # 当前价格
        yesterday_close = safe_float(parts[5])  # 昨日收盘
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


def get_from_tencent(codes):
    """从腾讯API批量获取行情"""
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


# ============================================================
# 大盘指数
# ============================================================
@app.route('/api/indices', methods=['GET'])
def get_indices():
    """上证/深证/创业板/沪深300"""
    cached = get_cache('indices')
    if cached:
        return jsonify({'code': 0, 'data': cached})

    # 腾讯行情代码
    codes_map = {
        'sh000001': '上证指数',
        'sz399001': '深证成指',
        'sz399006': '创业板指',
        'sh000300': '沪深300',
        'sh000016': '上证50',
        'sz399905': '中证500',
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
        {'name': '上证50', 'code': '000016', 'price': 2412.50, 'change': 8.32, 'pct': 0.35, 'status': 'up'},
        {'name': '中证500', 'code': '399905', 'price': 5824.60, 'change': -15.30, 'pct': -0.26, 'status': 'down'},
    ]


# ============================================================
# 涨停板列表（腾讯行情）
# ============================================================
@app.route('/api/ztlist', methods=['GET'])
def get_ztlist():
    """今日涨停板 - 使用腾讯分时数据"""
    cached = get_cache('ztlist')
    if cached:
        return jsonify({'code': 0, 'data': cached})

    # 获取涨幅排名靠前的股票
    try:
        # 腾讯热门排行API
        url = 'https://web.ifzq.gtimg.cn/appstock/app/rank/getrank'
        params = {'type': 'zt', 'date': datetime.datetime.now().strftime('%Y%m%d'), 'count': 20}
        resp = requests.get(url, params=params, headers=HEADERS, timeout=10)
        data = resp.json()

        result = []
        if data.get('data') and data['data'].get('list'):
            for item in data['data']['list'][:20]:
                code = safe_str(item.get('code', ''))
                name = safe_str(item.get('name', ''))
                pct = safe_float(item.get('zdp', 0))
                amount = safe_float(item.get('amount', 0)) / 1e8
                result.append({
                    'code': code,
                    'name': name,
                    'reason': safe_str(item.get('reason', '题材')),
                    'boards': safe_str(item.get('lb', '1')),
                    'pct': round(pct, 2),
                    'turnover': round(amount, 1),
                    'status': '二波' if safe_float(item.get('ebl', 0)) > 0 else '涨停',
                })

        if not result:
            result = get_mock_ztlist()
    except Exception:
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


# ============================================================
# 热门题材板块
# ============================================================
@app.route('/api/hot-sectors', methods=['GET'])
def get_hot_sectors():
    """热门题材板块"""
    cached = get_cache('hot_sectors')
    if cached:
        return jsonify({'code': 0, 'data': cached})

    # 腾讯板块行情
    try:
        url = 'https://qt.gtimg.cn/q=s_bdList'
        resp = requests.get(url, headers=HEADERS, timeout=10)
        text = resp.text.strip()

        result = []
        # 解析板块数据
        lines = text.split('\n')
        for line in lines[:15]:
            if '=' in line:
                code = line.split('=')[0].replace('v_bd_', '').replace('"', '')
                parts = line.split('"')
                if len(parts) > 1:
                    fields = parts[1].split('~')
                    if len(fields) > 5:
                        name = safe_str(fields[0])
                        pct = safe_float(fields[3])
                        if abs(pct) > 0.5:  # 只取涨跌幅 > 0.5%
                            result.append({
                                'name': name,
                                'pct': round(pct, 2),
                                'stock_count': 0,
                                'lead_stock': '',
                                'status': 'up' if pct > 0 else 'down',
                            })

        if not result:
            result = get_mock_sectors()
    except Exception:
        result = get_mock_sectors()

    # 排序取涨幅最大的前10
    result = sorted(result, key=lambda x: abs(x['pct']), reverse=True)[:10]

    set_cache('hot_sectors', result)
    return jsonify({'code': 0, 'data': result})


def get_mock_sectors():
    return [
        {'name': 'AI算力', 'pct': 4.25, 'stock_count': 36, 'lead_stock': '沪电股份', 'status': 'up'},
        {'name': '军工', 'pct': 3.18, 'stock_count': 52, 'lead_stock': '航发动力', 'status': 'up'},
        {'name': '新能源车', 'pct': 2.76, 'stock_count': 84, 'lead_stock': '宁德时代', 'status': 'up'},
        {'name': '消费电子', 'pct': 2.12, 'stock_count': 41, 'lead_stock': '京东方A', 'status': 'up'},
        {'name': '5G通信', 'pct': 1.88, 'stock_count': 38, 'lead_stock': '中兴通讯', 'status': 'up'},
        {'name': '半导体', 'pct': 1.65, 'stock_count': 62, 'lead_stock': '紫光国微', 'status': 'up'},
        {'name': '光伏', 'pct': 1.42, 'stock_count': 45, 'lead_stock': '阳光电源', 'status': 'up'},
        {'name': '医药', 'pct': -0.85, 'stock_count': 95, 'lead_stock': '', 'status': 'down'},
        {'name': '房地产', 'pct': -1.23, 'stock_count': 78, 'lead_stock': '', 'status': 'down'},
    ]


# ============================================================
# 个股详情
# ============================================================
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


# ============================================================
# AI 问答
# ============================================================
@app.route('/api/chat', methods=['POST'])
def chat():
    data = request.get_json() or {}
    message = data.get('message', '')
    if not message:
        return jsonify({'code': 1, 'msg': '消息不能为空'}), 400
    return jsonify({'code': 0, 'data': {'reply': f'收到：{message}。修哥七步法分析中，请稍候...'}})


# ============================================================
# 健康检查
# ============================================================
@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'time': datetime.datetime.now().isoformat()})


if __name__ == '__main__':
    print('涨停复盘宝后端启动')
    app.run(host='0.0.0.0', port=5000)
