"""
Flask 后端 - 涨停复盘宝
提供股票数据 API 和 AI 智能问答
"""

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import json
import os
import datetime
import akshare as ak
import sqlite3
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__, static_folder='../static')
CORS(app)

EXECUTOR = ThreadPoolExecutor(max_workers=5)

# ============================================================
# 股票数据缓存（避免频繁请求）
# ============================================================
CACHE = {
    'indices': {'data': None, 'time': None},
    'ztlist': {'data': None, 'time': None},
    'hot_sectors': {'data': None, 'time': None},
}
CACHE_TTL = 120  # 2分钟缓存


def get_cache(key):
    """检查缓存是否有效"""
    entry = CACHE.get(key)
    if entry and entry['time']:
        if (datetime.datetime.now() - entry['time']).seconds < CACHE_TTL:
            return entry['data']
    return None


def set_cache(key, data):
    CACHE[key] = {'data': data, 'time': datetime.datetime.now()}


# ============================================================
# 股票数据 API（akshare）
# ============================================================

@app.route('/api/indices', methods=['GET'])
def get_indices():
    """大盘指数（上证/深证/创业板/沪深300）"""
    cached = get_cache('indices')
    if cached:
        return jsonify({'code': 0, 'data': cached})

    def fetch():
        try:
            indices = [
                {'code': '000001', 'name': '上证指数', 'market': 'sh'},
                {'code': '399001', 'name': '深证成指', 'market': 'sz'},
                {'code': '399006', 'name': '创业板指', 'market': 'sz'},
                {'code': '000300', 'name': '沪深300', 'market': 'sh'},
            ]
            result = []
            for idx in indices:
                try:
                    df = ak.stock_zh_index_spot()
                    row = df[df['代码'] == idx['code']]
                    if not row.empty:
                        price = float(row['最新价'].values[0])
                        change = float(row['涨跌额'].values[0])
                        pct = float(row['涨跌幅'].values[0])
                        result.append({
                            'name': idx['name'],
                            'code': idx['code'],
                            'price': price,
                            'change': change,
                            'pct': pct,
                            'status': 'up' if change > 0 else 'down' if change < 0 else 'flat'
                        })
                except Exception:
                    pass
            return result
        except Exception as e:
            return []

    try:
        data = fetch()
        set_cache('indices', data)
        return jsonify({'code': 0, 'data': data})
    except Exception as e:
        return jsonify({'code': 1, 'msg': str(e)}), 500


@app.route('/api/ztlist', methods=['GET'])
def get_ztlist():
    """今日涨停板列表"""
    cached = get_cache('ztlist')
    if cached:
        return jsonify({'code': 0, 'data': cached})

    def fetch():
        try:
            # 获取今日涨停股
            df = ak.stock_zt_pool_em(date=datetime.datetime.now().strftime('%Y%m%d'))
            if df is None or df.empty:
                return []
            # 排序：按连板数降序
            df = df.sort_values('连板数', ascending=False)
            result = []
            for _, row in df.head(20).iterrows():
                result.append({
                    'code': str(row.get('代码', '')),
                    'name': str(row.get('名称', '')),
                    'reason': str(row.get('涨停统计[{}]'.format(datetime.datetime.now().strftime('%Y%m%d')), '题材')),  # 题材
                    'boards': int(row.get('连板数', 0)),
                    'pct': float(row.get('涨幅', 0)),
                    'turnover': float(row.get('成交额', 0)) / 1e8,  # 亿
                    'status': '二波' if int(row.get('连板数', 0)) == 0 and float(row.get('涨幅', 0)) >= 9 else '涨停',
                })
            return result
        except Exception as e:
            return []

    try:
        data = fetch()
        set_cache('ztlist', data)
        return jsonify({'code': 0, 'data': data})
    except Exception as e:
        return jsonify({'code': 1, 'msg': str(e)}), 500


@app.route('/api/hot-sectors', methods=['GET'])
def get_hot_sectors():
    """热门题材板块"""
    cached = get_cache('hot_sectors')
    if cached:
        return jsonify({'code': 0, 'data': cached})

    def fetch():
        try:
            df = ak.stock_board_industry_name_em()
            if df is None or df.empty:
                return []
            # 按涨跌幅排序，取前10
            df = df.sort_values('涨跌幅', ascending=False).head(10)
            result = []
            for _, row in df.iterrows():
                result.append({
                    'name': str(row.get('板块名称', '')),
                    'pct': float(row.get('涨跌幅', 0)),
                    'stock_count': int(row.get('总股票数', 0)),
                    'lead_stock': str(row.get('领涨股票', '')),
                    'status': 'up' if float(row.get('涨跌幅', 0)) > 0 else 'down',
                })
            return result
        except Exception as e:
            return []

    try:
        data = fetch()
        set_cache('hot_sectors', data)
        return jsonify({'code': 0, 'data': data})
    except Exception as e:
        return jsonify({'code': 1, 'msg': str(e)}), 500


@app.route('/api/stock/<code>', methods=['GET'])
def get_stock_detail(code):
    """个股详情（MA55、题材、资金流）"""
    try:
        # 获取个股K线数据
        df = ak.stock_zh_a_hist(symbol=code, period='daily', adjust='qfq')
        if df is None or df.empty:
            return jsonify({'code': 1, 'msg': '无数据'}), 404

        # 计算MA55
        df['MA55'] = df['收盘'].rolling(55).mean()
        latest = df.iloc[-1]
        ma55 = df['MA55'].iloc[-1] if not pd.isna(df['MA55'].iloc[-1]) else None

        return jsonify({
            'code': 0,
            'data': {
                'name': latest.get('股票代码', code),
                'price': float(latest.get('收盘', 0)),
                'pct': float(latest.get('涨跌幅', 0)),
                'ma55': float(ma55) if ma55 else None,
                'volume_ratio': float(latest.get('量比', 0)),
                'turnover': float(latest.get('成交额', 0)) / 1e8,
            }
        })
    except Exception as e:
        return jsonify({'code': 1, 'msg': str(e)}), 500


@app.route('/api/chat', methods=['POST'])
def chat():
    """AI 智能问答（通过 OpenClaw）"""
    data = request.get_json()
    message = data.get('message', '')
    if not message:
        return jsonify({'code': 1, 'msg': '消息不能为空'}), 400

    # TODO: 接入 OpenClaw
    # 暂时返回模拟回答
    return jsonify({
        'code': 0,
        'data': {
            'reply': f'收到您的问题：{message}。修哥交易系统七步法正在分析中...'
        }
    })


@app.route('/api/health', methods=['GET'])
def health():
    """健康检查"""
    return jsonify({'status': 'ok', 'time': datetime.datetime.now().isoformat()})


# ============================================================
# 静态文件
# ============================================================
@app.route('/')
def index():
    return send_from_directory('.', 'index.html')


@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory('../static', filename)


# ============================================================
# 启动
# ============================================================
if __name__ == '__main__':
    print('🚀 涨停复盘宝后端启动中...')
    print(f'   时间: {datetime.datetime.now()}')
    print(f'   服务: http://0.0.0.0:5000')
    app.run(host='0.0.0.0', port=5000, debug=False)
