#!/usr/bin/env python3
"""网页棋盘 ↔ 原生 Rapfi 桥接（v2：每步同步局面，杜绝错位）

关键设计：
  * 每个落子都先用 YXBOARD 把完整局面推给引擎，再发 TURN。
    YXBOARD 是 BOARD 的"不思考"版本（getPosition(false)），
    而 BOARD 会无条件让引擎思考并落子，即使不该它走 —— 那会导致
    引擎棋盘与前端棋盘错位，表现为「AI 乱下」最后「卡死」。
  * 任何超时都会重启引擎并回报错误，前端据此自动重新同步。

客户端消息：
  {"cmd":"new",     "human":1|2, "threads":6, "timeoutMs":800}
  {"cmd":"move",    "r":int, "c":int, "human":1|2, "stones":[[r,c,role],...]}
  {"cmd":"restore", "human":1|2, "stones":[...], "engineTurn":bool}
  {"cmd":"ping"}
服务端消息：
  {"type":"move","r":..,"c":..,"role":1|2,"info":{...}}
  {"type":"ready"} / {"type":"error","message":"..","fatal":bool}
"""
import asyncio
import json
import math
import os
import queue
import re
import subprocess
import threading
import time

import websockets

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(HERE, 'pbrain-rapfi')
PORT = 8766
MOVE_RE = re.compile(r'^(\d+),(\d+)$')
INFO_RE = re.compile(r'Depth (\S+) \| Eval (\S+) \| Node (\S+) \| Time (\S+)')
SPEED_RE = re.compile(r'Speed ([\d.]+)\s*([KMB]?)')


def scale_num(v, suf=''):
    """把引擎的缩写数字还原成实际值。

    官方发行的引擎印 "18K" / "Speed 874K" 这种带后缀的格式，
    而我们为手机自编译的版本印纯数字。两种都要能认，否则切换
    云端/本机时速度显示会差 1000 倍。
    """
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    mul = {'': 1, 'K': 1e3, 'M': 1e6, 'B': 1e9}.get(str(suf or '').upper(), 1)
    return int(n * mul)
DEPTH_LINE = re.compile(r'Depth (\S+) \| Eval (-?\S+) \| Time (\S+)')
# 单步等待上限**必须由档位推导**，不能写死。
# 踩过的坑：曾把这里固定成 5 秒想"少等一会儿"，结果「最强+ 6 秒」档
# 自己的硬上限就有 7.5 秒 —— 桥接先放弃了，页面报「引擎无应答」。
# 现在 = 该档位硬上限 + 余量（引擎可能还要跑完当前这一层迭代）。
MOVE_WAIT = 5.0          # 兜底默认值；实际用 max_wait() 计算

# 胜率换算（与引擎内部 Evaluation::valueToWinRate 完全一致）
#   winrate = 1 / (1 + e^(-eval / ScalingFactor))
# ScalingFactor = 200.0f（Rapfi/config.cpp），VALUE_MATE = 30000（core/types.h）
# 实测校验：eval -417 -> 0.11057，引擎自报 0.110563 ✓
SCALING_FACTOR = 200.0
VALUE_MATE = 30000
MATE_THRESHOLD = VALUE_MATE - 500      # VALUE_MATE_IN_MAX_PLY


def parse_ms(raw):
    """把引擎的 Time 字段（如 "1735ms"）转成毫秒数。"""
    m = re.match(r'([\d.]+)', str(raw or ''))
    try:
        return float(m.group(1)) if m else None
    except (TypeError, ValueError):
        return None


def parse_eval_str(raw):
    """把引擎的评估字符串转成整数分值。

    引擎的评估有两种写法：普通整数（如 -417）和必杀（如 +M5 / -M3）。
    之前只处理了整数，导致**必杀局面一条实时更新都推不出去**。
    """
    try:
        return int(raw)
    except (TypeError, ValueError):
        pass
    if isinstance(raw, str) and len(raw) > 2 and raw[0] in '+-' and raw[1] == 'M':
        try:
            n = int(raw[2:])
        except ValueError:
            return None
        return (VALUE_MATE - n) if raw[0] == '+' else (-VALUE_MATE + n)
    return None


def eval_to_winrate(e):
    """分值 -> 胜率（轮走方视角，0~1）。必杀区间返回精确 1.0/0.0。"""
    if e >= MATE_THRESHOLD:
        return 1.0
    if e <= -MATE_THRESHOLD:
        return 0.0
    return 1.0 / (1.0 + math.exp(-e / SCALING_FACTOR))


def parse_info(lines, eng_role=1):
    """从引擎输出里提取搜索信息。

    胜率口径：Rapfi 的 Eval 是**轮走方视角**（negamax）。
    引擎走棋时轮走方就是引擎自己，所以要按 eng_role 翻成「黑棋胜率」，
    前端才能用同一个坐标系画曲线。
    """
    info = {}
    ev = None
    for line in lines:
        m = INFO_RE.search(line)
        if m:
            info['depth'], info['eval'], _nodes_raw, info['ms'] = m.groups()
            _nm = re.match(r'([\d.]+)\s*([KMB]?)', str(_nodes_raw or ''))
            if _nm:
                _n = scale_num(_nm.group(1), _nm.group(2))
                if _n is not None:
                    info['nodes'] = _n
            if 'nodes' not in info:
                info['nodes'] = _nodes_raw
        s = SPEED_RE.search(line)
        if s:
            info['nps'] = scale_num(s.group(1), s.group(2))

    raw = info.get('eval')
    if raw is not None:
        ev = parse_eval_str(raw)
    if ev is not None:
        wr = eval_to_winrate(ev)
        info['winrate'] = round(wr, 4)                       # 轮走方胜率
        info['blackWr'] = round(wr if eng_role == 1 else 1.0 - wr, 4)
        if abs(ev) >= MATE_THRESHOLD:
            info['mate'] = {'side': 'black' if (ev > 0) == (eng_role == 1) else 'white',
                            'plies': VALUE_MATE - abs(ev)}
    return info


class Engine:
    def __init__(self):
        self.proc = None
        self.q = queue.Queue()
        self.threads = 6
        self.timeout_ms = 800
        self.eng_role = 1      # 1=黑 2=白，用于把胜率翻成黑棋视角
        self.warmed = False    # 权重是否已载入（载入过就能复用进程）
        self.strength = 100    # 0~100，100=满强度；调低会按评估加随机性地选着
        self.rule = 0          # 0=无禁手 1=标准(黑有禁手) 2/4=连珠 5/6=swap
        self.rule_loaded = None  # 已把权重载入内存的规则

    def start(self, threads=None, timeout_ms=None, force=False):
        if threads:
            self.threads = threads
        if timeout_ms:
            self.timeout_ms = timeout_ms

        # 复用常驻引擎：跳过进程重启 + NNUE 权重加载（约 1 秒），
        # 权重始终是热的，搜索性能和冷启动完全一致（甚至更好，置换表还在）。
        if (not force and self.warmed
                and self.proc is not None and self.proc.poll() is None):
            self.flush(0.02)
            self.send('START 15')
            self.send('INFO rule %d' % self.rule)
            self.send('INFO STRENGTH %d' % self.strength)
            self.send('INFO thread_num %d' % self.threads)
            self.send('INFO timeout_turn %d' % self.quota_ms())
            self.send('INFO SHOW_DETAIL 2')
            self.wait_ready(0.8)
            print('[复用] 引擎已在运行，跳过重启与权重加载', flush=True)
            return

        self.stop()
        self.warmed = False
        self.rule_loaded = None      # 新进程权重未载入，必须让 new 重新预热
        # 清掉可能残留的孤儿引擎进程
        try:
            subprocess.run(['pkill', '-f', 'pbrain-rapfi'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            time.sleep(0.2)
        except Exception:
            pass
        self.q = queue.Queue()
        self.proc = subprocess.Popen(
            [ENGINE], cwd=HERE,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1)
        threading.Thread(target=self._reader, daemon=True).start()
        self.send('START 15')
        self.send('INFO rule %d' % self.rule)
        self.send('INFO STRENGTH %d' % self.strength)
        self.send('INFO thread_num %d' % self.threads)
        self.send('INFO timeout_turn %d' % self.quota_ms())
        self.send('INFO SHOW_DETAIL 2')
        self.wait_ready(2.0)
        self.warmup()

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def _reader(self):
        try:
            for line in self.proc.stdout:
                self.q.put(line.rstrip('\n'))
        except Exception:
            pass

    def send(self, cmd):
        if not self.alive():
            raise RuntimeError('引擎未运行')
        self.proc.stdin.write(cmd + '\n')
        self.proc.stdin.flush()

    def quota_ms(self):
        """发给引擎的内部时间配额 = 用户档位 × 2。

        原因：config 里 advanced_stop_ratio = 0.5 会让引擎「用掉一半时间
        就退出搜索」（注释原文：Exit search if turn time is used more than
        this ratio）。实测设定 3000ms 只用 1.53s（0.51x）、设定 6000ms 只用
        3.58s（0.60x），深度因此少了好几层。

        那个参数本身不能动：实测 0.7 会让引擎崩溃、0.9 会让它卡住不返回。
        所以改成把配额给两倍 —— 它砍一半，正好回到用户要的时间，而搜索
        过程中它「以为」有双倍时间，会搜得更深。

        实测收益：3000ms 档深度 10-12 → 13-27；6000ms 档 14-27 → 18-19。
        """
        return int(self.timeout_ms * 2)

    def soft_cap(self):
        """硬上限（秒）：超过就发 STOP 让引擎立刻交子。

        引擎自己的停止检查只发生在两次深度迭代**之间**，而单次迭代可能要几秒，
        所以偶尔会远超设定时限（实测：设 1 秒跑出 5.5 秒）。STOP 就是兜这个尾巴：
        引擎收到后会带着"已完成深度里的最优着"立即返回，不会丢子。
        """
        # 硬上限只是「预测失效」时的兜底（层进度来得太晚 / 根本没来）。
        # 既然 STOP 会保留已完成的层，砍掉一个跑不完的层没有任何损失，
        # 所以可以收得紧。注意用纯倍数：原来的 `1.4x+0.6` 里那个 +0.6 秒
        # 会把小档位撑大（400ms 档实际给了 1.16s，2.9 倍），这正是
        # 「极速档反而不快」的原因。
        t = self.timeout_ms / 1000.0
        # 上限只是兜底：引擎通常在自己算出的时间点就退了（约为档位的
        # 1.05~1.3 倍），所以这里放到 1.45 倍，避免把它的正常退出打断。
        return max(t * 1.45, t + 0.5)

    def consider_deadline(self, line, deadline, prev_t, factor=1.0):
        # 大档位（>=1.5 秒）不启用「预测下一层跑不完就停」：用户选它就是要深度，
        # 宁可多等一层也不要少搜一层。小档位（极速/标准）才用预测换速度。
        if deadline >= 1.5:
            factor = 0.0
        """基于「层进度」的精确时间控制 —— 这是解决超时的关键。

        引擎自己的停止检查只在两次深度迭代**之间**做，而一层可能要好几秒，
        所以它经常跑飞（实测：设 400ms 却跑了 1400ms）。而引擎每完成一层
        都会打印一行 `Depth x-y | Eval v | Time t`，t 就是**跑完这层的用时**。
        利用它就能卡在刚好够用的时刻：

          规则1  已完成层的用时 >= 目标        → 立刻停（下一层只会更慢）
          规则2  预测下一层跑不完（t + 增量*f > 目标）→ 也停，保留当前层最优着

        效果：既不白等一个跑不完的层，也不会被引擎拖到几倍时间。
        """
        dm = DEPTH_LINE.search(line)
        if not dm:
            return False
        t = parse_ms(dm.group(3))
        if t is None:
            return False
        target = deadline * 1000.0
        if t >= target:
            try:
                self.send('STOP')
            except Exception:
                pass
            print('[限时] 层用时 %.0fms 已达目标 %.0fms，STOP' % (t, target), flush=True)
            return True
        if prev_t and t > prev_t:
            # 下一层用时估计：至少是「上一层的用时增量」，且至少是「当前总用时」的
            # 0.9 倍。依据实测的层增长曲线（1.2~2.9 倍/层）——纯用增量会低估，
            # 纯用倍数会在增长陡增处过度保守，两者取大更稳。
            inc = t - prev_t
            est = max(inc, 0.9 * t) * factor
            if t + est > target:
                try:
                    self.send('STOP')
                except Exception:
                    pass
                print('[限时] 预测下一层跑不完（%.0f+%.0f>%.0f），提前 STOP'
                      % (t, est, target), flush=True)
                return True
        return False

    def max_wait(self):
        """单步最多等多久：档位硬上限 + 余量。

        写死会出事（5 秒 < 最强+ 档的 7.5 秒上限 → 误报无应答）。
        """
        return self.soft_cap() + 8.0

    def wait_move(self, timeout=MOVE_WAIT, soft_cap=None, deadline=None):
        t0 = time.time()
        infos = []
        stopped = False
        if soft_cap is None:
            soft_cap = self.soft_cap()
        if deadline is None:
            deadline = self.timeout_ms / 1000.0
        prev_depth_t = None
        stop_sent = 0.0
        while True:
            el = time.time() - t0
            if el >= timeout:
                break
            if not stopped and el >= soft_cap:
                stopped = True
                stop_sent = el
                try:
                    self.send('STOP')
                    print('[限时] 超 %.1fs 未交子，已发 STOP' % el, flush=True)
                except Exception:
                    pass
            elif stopped and el - stop_sent >= 0.25:
                # 补发 STOP：引擎只在「两次深度迭代之间」检查停止，
                # 单个 STOP 恰好吃在一次超长迭代里时要等它跑完才生效
                # （实测会拖到十几秒甚至不交子）。每 0.25s 补一发，
                # 只要它有任何一个检查点到来就能立刻停住。
                stop_sent = el
                try:
                    self.send('STOP')
                    print('[限时] 补发 STOP（%.1fs）' % el, flush=True)
                except Exception:
                    pass
            try:
                line = self.q.get(timeout=0.1)
            except queue.Empty:
                continue
            s = line.strip()
            m = MOVE_RE.match(s)
            if m:
                info = parse_info(infos, self.eng_role)
                if stopped:
                    info['stopped'] = True
                return (int(m.group(2)), int(m.group(1))), info
            if s and s != 'OK':
                infos.append(s)
                if not stopped:
                    before = stopped
                    stopped = self.consider_deadline(s, deadline, prev_depth_t)
                    if stopped:
                        prev_depth_t = None
                    else:
                        dm = DEPTH_LINE.search(s)
                        if dm:
                            t = parse_ms(dm.group(3))
                            if t is not None:
                                prev_depth_t = t
        return None, parse_info(infos, self.eng_role)

    def soft_reset(self):
        """把引擎复位到「刚开好局」的干净状态。

        取候选会用到 BOARD / YXNBEST / YXBLOCK 这类命令，跑完后
        引擎会残留状态，实测会干扰紧接着的正常搜索（表现为无应答）。
        用 START 重新初始化最省事。
        """
        try:
            self.send('START 15')
            self.send('INFO rule %d' % self.rule)
            self.send('INFO STRENGTH %d' % self.strength)
            self.send('INFO thread_num %d' % self.threads)
            self.send('INFO timeout_turn %d' % self.quota_ms())
            self.send('INFO SHOW_DETAIL 2')
            self.flush(0.15)
            print('[复位] 取候选后已复位引擎', flush=True)
        except Exception as e:
            print('[复位失败]', e, flush=True)

    async def wait_move_live(self, ws, side, timeout=MOVE_WAIT, soft_cap=None,
                             deadline=None):
        """边搜边推：每完成一层就把胜率推给前端，实现「实时分析」。

        页面摆完子立刻就能看到当前胜率，然后随搜索加深不断修正，
        而不是傻等 2~5 秒才出结果。

        停止策略与 wait_move 保持一致（层进度 + 预测 + 补发 STOP）——
        之前这条路径漏了，只会死等硬上限，白白多花时间。
        """
        t0 = time.time()
        raw = []
        stopped = False
        stop_sent = 0.0
        prev_depth_t = None
        last_sent = 0.0
        if soft_cap is None:
            soft_cap = self.soft_cap()
        if deadline is None:
            deadline = self.timeout_ms / 1000.0
        while True:
            el = time.time() - t0
            if el >= timeout:
                break
            if not stopped and el >= soft_cap:
                stopped = True
                stop_sent = el
                try:
                    self.send('STOP')
                except Exception:
                    pass
            elif stopped and el - stop_sent >= 0.25:
                stop_sent = el
                try:
                    self.send('STOP')
                except Exception:
                    pass
            try:
                line = self.q.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.03)
                continue
            s2 = line.strip()
            m = MOVE_RE.match(s2)
            if m:
                return ((int(m.group(2)), int(m.group(1))), raw,
                        parse_info(raw, side))
            if s2 and s2 != 'OK':
                raw.append(s2)
                if not stopped:
                    stopped = self.consider_deadline(s2, deadline, prev_depth_t)
                    if stopped:
                        prev_depth_t = None
                    else:
                        dm0 = DEPTH_LINE.search(s2)
                        if dm0:
                            t0d = parse_ms(dm0.group(3))
                            if t0d is not None:
                                prev_depth_t = t0d
                dm = DEPTH_LINE.search(s2)
                # 限流：最多每 120ms 推一次，免得刷屏
                if dm and time.time() - last_sent > 0.12:
                    last_sent = time.time()
                    ev = parse_eval_str(dm.group(2))
                    if ev is None:
                        continue
                    wr = eval_to_winrate(ev)
                    try:
                        await ws.send(json.dumps({
                            'type': 'live', 'depth': dm.group(1), 'eval': str(ev),
                            'blackWr': round(wr if side == 1 else 1.0 - wr, 4),
                            'ms': dm.group(3)}))
                    except Exception:
                        pass
        return None, raw, parse_info(raw, side)

    def wait_idle(self, timeout=3.0):
        """等引擎真正空闲。

        关键：Rapfi 的协议循环里有 `else if (thinking) return false;`
        —— **搜索进行中，除 STOP/END 外所有命令都被静默丢弃**。
        如果这时发 YXBOARD，棋盘不会被设上，而紧接着的坐标行会被
        当成独立命令 → 报 "Unknown command: 7,7,1" → 整个局面错乱，
        表现为「引擎无应答」或「下了个莫名其妙的位置」。

        探针用 YXSHOWFORBID：只有空闲时它才会输出 FORBID。
        """
        t0 = time.time()
        while time.time() - t0 < timeout:
            self.flush(0.01)
            try:
                self.send('YXSHOWFORBID')
            except Exception:
                return False
            t1 = time.time()
            while time.time() - t1 < 0.35:
                try:
                    line = self.q.get(timeout=0.05)
                except queue.Empty:
                    continue
                if 'FORBID' in line:
                    self.flush(0.02)
                    return True
            time.sleep(0.05)
        # 等不到空闲 = 引擎状态已经错位（比如命令被丢弃、或卡在后台思考）。
        # 绝不能带着这种状态继续跑，否则后续每一步都会跟着坏掉（级联失败）。
        # 直接重启到干净状态最稳妥。
        print('[警告] 等空闲超时 → 重启引擎恢复到干净状态', flush=True)
        try:
            self.start(force=True)
        except Exception as e:
            print('[重启失败]', e, flush=True)
        return False

    def wait_ready(self, timeout=2.0):
        """等引擎就绪（每条命令它都回 OK；START 的 OK 表示配置已加载）。
        比盲等固定毫秒更快也更稳。"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                line = self.q.get(timeout=0.05)
            except queue.Empty:
                continue
            if line.strip() == 'OK':
                return True
        return False

    def flush(self, sec=0.12):
        if sec:
            time.sleep(sec)
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                break

    def warmup(self):
        """先下一盘废棋，把 NNUE 权重载进内存（约 0.9 秒）。

        否则这笔加载费会算在用户的第一步上（设 0.8 秒却等了 1.7 秒）。
        预热放在「启动引擎」阶段，用户对那段等待有预期。
        """
        old = self.timeout_ms
        t0 = time.time()
        try:
            self.send('INFO timeout_turn 80')
            self.send('YXBOARD')
            self.send('DONE')
            self.send('BEGIN')
            while time.time() - t0 < 12:
                try:
                    line = self.q.get(timeout=0.2)
                except queue.Empty:
                    continue
                s = line.strip()
                if s and ',' in s and s.replace(',', '').isdigit():
                    break
            print('[预热] 权重载入完成 (%.1fs)' % (time.time() - t0), flush=True)
        except Exception as e:
            print('[预热失败]', e, flush=True)
        finally:
            self.flush(0.03)
            self.send('START 15')
            self.send('INFO rule %d' % self.rule)
            self.send('INFO STRENGTH %d' % self.strength)
            self.send('INFO thread_num %d' % self.threads)
            self.send('INFO timeout_turn %d' % self.quota_ms())
            self.wait_ready(1.0)
            self.warmed = True
            self.rule_loaded = self.rule

    def stop(self):
        if self.proc:
            try:
                self.proc.kill()
                self.proc.wait(timeout=3)
            except Exception:
                pass
            self.proc = None


ENG = Engine()
LOCK = asyncio.Lock()


async def call(fn, *a):
    return await asyncio.to_thread(fn, *a)


def send_many(lines):
    """把多条命令合并成一次写入。

    原来每颗子都 write+flush 一次（24 颗子就是 24 次系统调用），
    在 iSH 里系统调用不算便宜，合并成一次省掉这部分开销。
    """
    if not lines:
        return
    try:
        ENG.proc.stdin.write(''.join(l + '\n' for l in lines))
        ENG.proc.stdin.flush()
    except Exception:
        pass


def put_board(stones, eng_role, think):
    """把局面推给引擎。think=True 用 BOARD（会思考并落子），否则 YXBOARD（纯摆子）。"""
    ENG.wait_idle()          # 引擎在思考时 YXBOARD 会被丢弃，必须先确认空闲
    lines = ['BOARD' if think else 'YXBOARD']
    for st in stones:
        r, c, role = int(st[0]), int(st[1]), int(st[2])
        flag = 1 if role == eng_role else 2      # 1=自己 2=对手
        lines.append('%d,%d,%d' % (c, r, flag))
    lines.append('DONE')
    send_many(lines)


async def do_move(ws, msg, stones=None, retry=True):
    """人类落子：先同步局面，再让引擎应一手。失败会自动重启引擎重试一次。"""
    r, c = int(msg['r']), int(msg['c'])
    human = int(msg.get('human', 1))
    eng_role = 2 if human == 1 else 1
    ENG.eng_role = eng_role
    if stones is None:
        stones = msg.get('stones') or []
    ENG.flush(0.03)
    try:
        # 注意：stones 为空时也必须重置棋盘，否则引擎会带着上一局的盘面
        # 去接这一手的 TURN（实测会卡住不交子）。
        put_board(stones if stones else [], eng_role, False)
            # 这里原来有个 flush(0.10) 固定睡 100ms，是调试期担心
            # "棋盘还没设好就发 TURN"留下的。stdin 写入本身有序、引擎按序
            # 处理命令，这个等待纯属浪费，已去掉。
        ENG.send('TURN %d,%d' % (c, r))
        mv, info = await call(ENG.wait_move, ENG.max_wait())
    except Exception as e:
        print('[落子异常]', e, flush=True)
        mv, info = None, {}
    if mv:
        await ws.send(json.dumps({'type': 'move', 'r': mv[0], 'c': mv[1],
                                  'role': eng_role, 'info': info}))
        return
    if retry:
        print('[超时] 重启引擎并重试  r=%d c=%d stones=%d' % (r, c, len(stones)), flush=True)
        try:
            await call(ENG.start, None, None, True)
        except Exception as e:
            print('[重启失败]', e, flush=True)
        await ws.send(json.dumps({'type': 'error', 'fatal': False,
                                  'message': '引擎无应答，已重启并重试'}))
        return await do_move(ws, msg, stones, retry=False)
    await ws.send(json.dumps({'type': 'error', 'fatal': True,
                              'message': '引擎连续无应答，请点"重新开始"'}))


async def handle(ws):
    peer = ws.remote_address
    print('[连接]', peer, flush=True)
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            cmd = msg.get("cmd")
            print("[命令] %s %s" % (cmd, {k: v for k, v in msg.items() if k not in ("stones",)}), flush=True)
            try:
                async with LOCK:
                    if cmd == 'ping':
                        await ws.send(json.dumps({'type': 'ready'}))

                    elif cmd == 'new':
                        human = int(msg.get('human', 1))
                        ENG.eng_role = 2 if human == 1 else 1
                        if msg.get('strength') is not None:
                            ENG.strength = max(0, min(100, int(msg['strength'])))
                        new_rule = int(msg.get('rule', ENG.rule))
                        if new_rule != ENG.rule:
                            ENG.rule = new_rule
                            print('[规则] 切到 %d' % ENG.rule, flush=True)
                        await call(ENG.start, int(msg.get('threads', 6)),
                                   int(msg.get('timeoutMs', 800)))
                        if ENG.rule_loaded != ENG.rule:
                            await call(ENG.warmup)   # 把新规则的权重载进来
                        fm = msg.get('firstMove')
                        if human == 2 and fm:
                            # 用户指定了 AI 的第一手：直接摆上去，不让引擎自己选开局
                            fr, fc = int(fm[0]), int(fm[1])
                            put_board([[fr, fc, ENG.eng_role]], ENG.eng_role, False)
                            ENG.flush(0.12)
                            print('[开局] AI 首子由用户指定 (%d,%d)' % (fr, fc), flush=True)
                            await ws.send(json.dumps(
                                {'type': 'move', 'r': fr, 'c': fc, 'role': 1,
                                 'info': {'opening': True}}))
                        elif human == 2:
                            ENG.send('BEGIN')
                            mv, info = await call(ENG.wait_move, MOVE_WAIT)
                            if mv:
                                await ws.send(json.dumps(
                                    {'type': 'move', 'r': mv[0], 'c': mv[1],
                                     'role': 1, 'info': info}))
                            else:
                                await ws.send(json.dumps(
                                    {'type': 'error', 'fatal': True,
                                     'message': '引擎无应答'}))
                        else:
                            await ws.send(json.dumps({'type': 'ready'}))

                    elif cmd == 'move':
                        await do_move(ws, msg)

                    elif cmd == 'restore':
                        human = int(msg.get('human', 1))
                        eng_role = 2 if human == 1 else 1
                        ENG.eng_role = eng_role
                        if msg.get('timeoutMs') and ENG.alive():
                            ENG.timeout_ms = int(msg['timeoutMs'])
                            ENG.send('INFO timeout_turn %d' % ENG.quota_ms())
                        if msg.get('rule') is not None and int(msg['rule']) != ENG.rule:
                            ENG.rule = int(msg['rule'])
                            if ENG.alive():
                                ENG.send('INFO rule %d' % ENG.rule)
                        stones = msg.get('stones') or []
                        ENG.flush(0.03)
                        if msg.get('engineTurn'):
                            put_board(stones, eng_role, True)   # BOARD：摆完直接思考
                            mv, info = await call(ENG.wait_move, MOVE_WAIT)
                            if mv:
                                await ws.send(json.dumps(
                                    {'type': 'move', 'r': mv[0], 'c': mv[1],
                                     'role': eng_role, 'info': info}))
                            else:
                                await ws.send(json.dumps(
                                    {'type': 'error', 'fatal': True,
                                     'message': '引擎无应答'}))
                        else:
                            put_board(stones, eng_role, False)  # YXBOARD：只摆子
                            ENG.flush(0.08)
                            await ws.send(json.dumps({'type': 'ready'}))

                    elif cmd == 'setopt':
                        # 热切换：直接改引擎的选项，不重启进程
                        if msg.get('timeoutMs'):
                            ENG.timeout_ms = int(msg['timeoutMs'])
                        if msg.get('threads'):
                            ENG.threads = int(msg['threads'])
                        if msg.get('strength') is not None:
                            ENG.strength = max(0, min(100, int(msg['strength'])))
                        new_rule = int(msg.get('rule', ENG.rule))
                        rule_changed = (new_rule != ENG.rule)
                        ENG.rule = new_rule
                        if ENG.alive():
                            ENG.send('INFO timeout_turn %d' % ENG.quota_ms())
                            if rule_changed:
                                ENG.send('INFO rule %d' % ENG.rule)
                                # 换规则要重新载入对应 NNUE 权重（约 1 秒），
                                # 在这里先付掉，用户第一步就不会卡
                                await call(ENG.warmup)
                            else:
                                ENG.send('INFO STRENGTH %d' % ENG.strength)
                                ENG.send('INFO thread_num %d' % ENG.threads)
                                ENG.flush(0.05)
                        print('[设置] 规则 %d  时限 %dms  线程 %d%s' % (
                            ENG.rule, ENG.timeout_ms, ENG.threads,
                            '（已重载权重）' if rule_changed else ''), flush=True)
                        try:
                            await ws.send(json.dumps({'type': 'ready'}))
                        except Exception:
                            pass

                    elif cmd == 'hint':
                        # 取「轮到的那一方」的前 N 个候选着：
                        # 先封锁当前最优着再搜一次，得到的就是第二候选，以此类推。
                        # 引擎站在 side 那一方的视角思考（把 side 标成 SELF）。
                        side = int(msg.get('side', 1))
                        n = max(1, min(5, int(msg.get('n', 3))))
                        hstones = msg.get('stones') or []
                        ENG.eng_role = side
                        ENG.flush(0.05)
                        try:
                            ENG.send('YXBLOCKRESET')
                            out = []
                            for k in range(n):
                                ENG.wait_idle()
                                if hstones:
                                    put_board(hstones, side, False)
                                else:
                                    ENG.send('YXBOARD')
                                    ENG.send('DONE')
                                ENG.flush(0.12)
                                ENG.send('YXNBEST 1')
                                # 第 1 候选跑满时限（数值要准）；
                                # 第 2/3 候选只需「给出另一个可行点」，长考没意义，
                                # 压到 1 秒，整体耗时从 3×时限 降到 1×时限 + 2 秒
                                # 首候选最多 2.5 秒（分清强弱足够；高档位跑满
                                # 会让取候选等到 8 秒以上）；2/3 候选 1 秒即可
                                # 分析预算：优先用前端指定的 ms（军师模式看的是
                                # 判断准不准，不该被对局用的「时限档位」拖成浅搜索）。
                                # 没指定就沿用时限档位。
                                hbase = (int(msg['ms']) / 1000.0) if msg.get('ms') \
                                        else (ENG.timeout_ms / 1000.0)
                                if k == 0:
                                    cap = hbase            # 首选着用满预算
                                else:
                                    cap = max(0.8, hbase * 0.4)   # 备选着给 40%
                                if k == 0:
                                    # 首个候选走流式：边搜边把胜率推给前端
                                    mv, _raw, info = await ENG.wait_move_live(
                                        ws, side, ENG.max_wait(), cap, cap)
                                else:
                                    mv, info = await call(ENG.wait_move, ENG.max_wait(), cap, cap)
                                if not mv:
                                    break
                                info['rank'] = k + 1
                                out.append({'r': mv[0], 'c': mv[1], 'info': info})
                                # 封锁这一个点，下一轮就得到次优
                                ENG.wait_idle()
                                ENG.send('YXBLOCK')
                                ENG.send('%d,%d' % (mv[1], mv[0]))
                                ENG.send('DONE')
                                ENG.flush(0.08)
                            await ws.send(json.dumps({'type': 'hints', 'list': out,
                                                      'side': side}))
                        except Exception as e:
                            print('[提示失败]', e, flush=True)
                            await ws.send(json.dumps({'type': 'error',
                                                      'message': '取候选着失败: %s' % e,
                                                      'fatal': False}))
                        finally:
                            # 无论成功失败都必须清空封锁列表，
                            # 否则残留的封锁会让引擎之后一直避开那些点
                            try:
                                ENG.send('YXBLOCKRESET')
                                ENG.flush(0.05)
                            except Exception:
                                pass
                            # 复位，避免取候选的残留状态干扰后续搜索
                            ENG.soft_reset()

                    elif cmd == 'stop':
                        ENG.stop()
                        await ws.send(json.dumps({'type': 'ready'}))
                    else:
                        await ws.send(json.dumps(
                            {'type': 'error', 'message': 'unknown cmd'}))
            except Exception as e:
                await ws.send(json.dumps({'type': 'error',
                                          'message': str(e), 'fatal': False}))
    except websockets.ConnectionClosed:
        pass
    finally:
        print('[断开]', peer, flush=True)


def already_running():
    """检查是不是已经有桥接在监听这个端口。

    背景：iSH 上 kill 常常不生效，反复重启会堆出多个实例，
    它们一起抢 CPU，把引擎饿到十几秒交不出子（表现为「引擎无应答」、
    预热时间从 0.9 秒涨到 12 秒）。所以启动前必须先确认没人占坑。
    """
    import socket
    sk = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sk.settimeout(1.0)
    try:
        return sk.connect_ex(('127.0.0.1', PORT)) == 0
    finally:
        sk.close()


async def main():
    if already_running():
        print('❌ 端口 %d 已被占用：已经有一个桥接在跑了。' % PORT, flush=True)
        print('   重复启动会互相抢 CPU，导致「引擎无应答」。' % (), flush=True)
        print('   如需重启：先跑 sh /root/rapfi/stop.sh，再跑 start.sh', flush=True)
        return
    print('桥接 v2 启动: ws://127.0.0.1:%d  引擎: %s' % (PORT, ENGINE), flush=True)
    # 启动即预热：把进程启动 + 权重加载这两笔开销提前到这里，
    # 用户点开局时引擎已经是热的（否则第一局要等 1.5 秒）。
    try:
        await asyncio.to_thread(ENG.start, 6, 800)
        print('[预热] 桥接就绪', flush=True)
    except Exception as e:
        print('[预热失败]', e, flush=True)
    async with websockets.serve(handle, '127.0.0.1', PORT, max_size=4*1024*1024):
        await asyncio.Future()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        ENG.stop()
