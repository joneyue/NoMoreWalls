#!/usr/bin/env python3
import yaml
import json
import base64
from urllib.parse import quote, unquote, urlparse
import requests
import datetime
import traceback
import binascii
import threading
import sys

try: PROXY = open("local_proxy.conf").read().strip()
except FileNotFoundError: LOCAL = False; PROXY = None
else: LOCAL = not PROXY

def b64encodes(s):
    return base64.b64encode(s.encode('utf-8')).decode('utf-8')

def b64encodes_safe(s):
    return base64.urlsafe_b64encode(s.encode('utf-8')).decode('utf-8')

def b64decodes(s):
    ss = s + '=' * ((4-len(s)%4)%4)
    try:
        return base64.b64decode(ss.encode('utf-8')).decode('utf-8')
    except UnicodeDecodeError: raise
    except binascii.Error: raise

def b64decodes_safe(s):
    ss = s + '=' * ((4-len(s)%4)%4)
    try:
        return base64.urlsafe_b64decode(ss.encode('utf-8')).decode('utf-8')
    except UnicodeDecodeError: raise
    except binascii.Error: raise

DEFAULT_UUID = '8'*8+'-8888'*3+'-'+'8'*12

CLASH2VMESS = {'name': 'ps', 'server': 'add', 'port': 'port', 'uuid': 'id', 
              'alterId': 'aid', 'cipher': 'scy', 'network': 'net', 'servername': 'sni'}
VMESS2CLASH = {}
for k,v in CLASH2VMESS.items(): VMESS2CLASH[v] = k

VMESS_EXAMPLE = {
    "v": "2", "ps": "", "add": "0.0.0.0", "port": "0", "aid": "0", "scy": "auto",
    "net": "tcp", "type": "none", "tls": "", "id": DEFAULT_UUID
}

CLASH_CIPHER_VMESS = "auto aes-128-gcm chacha20-poly1305 none"
CLASH_CIPHER_SS = "aes-128-gcm aes-192-gcm aes-256-gcm aes-128-cfb aes-192-cfb \
        aes-256-cfb aes-128-ctr aes-192-ctr aes-256-ctr rc4-md5 chacha20-ietf \
        xchacha20 chacha20-ietf-poly1305 xchacha20-ietf-poly1305".split()
CLASH_SSR_OBFS = "plain http_simple http_post random_head tls1.2_ticket_auth tls1.2_ticket_fastauth"
CLASH_SSR_PROTOCOL = "origin auth_sha1_v4 auth_aes128_md5 auth_aes128_sha1 auth_chain_a auth_chain_b"

class UnsupportedType(Exception): pass
class NotANode(Exception): pass

session: requests.Session
io_lock = threading.Lock()

class Node:
    def __init__(self, data) -> None:
        if isinstance(data, dict):
            self.data = data
            self.type = data['type']
        elif isinstance(data, str):
            self.load_url(data)
        else: raise TypeError
        if not self.data['name']:
            self.data['name'] = "未命名"
        if 'password' in self.data:
            self.data['password'] = str(self.data['password'])
        self.data['type'] = self.type

    def __str__(self):
        return self.url

    def __hash__(self):
        # return hash(f"{self.data['server']}:{self.data['port']}")
        return hash(self.data['server'])
    
    def __eq__(self, other):
        if isinstance(other, self.__class__):
            return hash(self) == hash(other)
        else:
            return False

    def load_url(self, url: str) -> None:
        try: self.type, dt = url.split("://")
        except ValueError: raise NotANode(url)
        # === Fix begin ===
        if not self.type.isascii():
            self.type = ''.join([_ for _ in self.type if _.isascii()])
            url = self.type+'://'+url.split("://")[1]
        # === Fix end ===
        if self.type == 'vmess':
            v = VMESS_EXAMPLE.copy()
            try: v.update(json.loads(b64decodes(dt)))
            except Exception:
                raise UnsupportedType('vmess', 'SP')
            self.data = {}
            for key, val in v.items():
                if key in VMESS2CLASH:
                    self.data[VMESS2CLASH[key]] = val
            self.data['tls'] = (v['tls'] == 'tls')
            self.data['alterId'] = int(self.data['alterId'])
            if v['net'] == 'ws':
                opts = {}
                if 'path' in v:
                    opts['path'] = v['path']
                if 'host' in v:
                    opts['headers'] = {'Host': v['host']}
                self.data['ws-opts'] = opts
            elif v['net'] == 'h2':
                opts = {}
                if 'path' in v:
                    opts['path'] = v['path']
                if 'host' in v:
                    opts['host'] = v.split(',')
                self.data['h2-opts'] = opts
            elif v['net'] == 'grpc' and 'path' in v:
                self.data['grpc-opts'] = {'grpc-service-name': v['path']}

        elif self.type == 'ss':
            info = url.split('@')
            srvname = info.pop()
            if '#' in srvname:
                srv, name = srvname.split('#')
            else:
                srv = srvname
                name = ''
            server, port = srv.split(':')
            try:
                port = int(port)
            except ValueError:
                raise UnsupportedType('ss', 'SP')
            info = '@'.join(info)
            if not ':' in info:
                info = b64decodes_safe(info)
            if ':' in info:
                cipher, passwd = info.split(':')
            else:
                cipher = info
                passwd = ''
            self.data = {'name': unquote(name), 'server': server, 
                    'port': port, 'type': 'ss', 'password': passwd, 'cipher': cipher}

        elif self.type == 'ssr':
            if '?' in url:
                parts = dt.split(':')
            else:
                parts = b64decodes_safe(dt).split(':')
            try:
                passwd, info = parts[-1].split('/?')
            except: raise
            passwd = b64decodes_safe(passwd)
            self.data = {'type': 'ssr', 'server': parts[0], 'port': parts[1],
                    'protocol': parts[2], 'cipher': parts[3], 'obfs': parts[4],
                    'password': passwd, 'name': ''}
            for kv in info.split('&'):
                k_v = kv.split('=')
                if len(k_v) != 2:
                    k = k_v[0]
                    v = ''
                else: k,v = k_v
                if k == 'remarks':
                    self.data['name'] = v
                elif k == 'group':
                    self.data['group'] = v
                elif k == 'obfsparam':
                    self.data['obfs-param'] = v
                elif k == 'protoparam':
                    self.data['protocol-param'] = v

        elif self.type == 'trojan':
            parsed = urlparse(url)
            self.data = {'name': unquote(parsed.fragment), 'server': parsed.hostname, 
                    'port': parsed.port, 'type': 'trojan', 'password': unquote(parsed.username)}
            if parsed.query:
                for kv in parsed.query.split('&'):
                    k,v = kv.split('=')
                    if k == 'allowInsecure':
                        self.data['skip-cert-verify'] = (v != 0)
                    elif k == 'sni': self.data['sni'] = v
                    elif k == 'alpn':
                        if '%2C' in v:
                            self.data['alpn'] = ["h2", "http/1.1"]
                        else:
                            self.data['alpn'] = [v]
                    elif k == 'type':
                        self.data['network'] = v
                    elif k == 'serviceName':
                        if 'grpc-opts' not in self.data:
                            self.data['grpc-opts'] = {}
                        self.data['grpc-opts']['grpc-service-name'] = v
                    elif k == 'host':
                        if 'ws-opts' not in self.data:
                            self.data['ws-opts'] = {}
                        if 'headers' not in self.data['ws-opts']:
                            self.data['ws-opts']['headers'] = {}
                        self.data['ws-opts']['headers']['Host'] = v
                    elif k == 'path':
                        if 'ws-opts' not in self.data:
                            self.data['ws-opts'] = {}
                        self.data['ws-opts']['path'] = v
        
        else: raise UnsupportedType(self.type)

    @property
    def url(self) -> str:
        data = self.data
        if self.type == 'vmess':
            v = VMESS_EXAMPLE.copy()
            for key,val in data.items():
                if key in CLASH2VMESS:
                    v[CLASH2VMESS[key]] = val
            if v['net'] == 'ws':
                if 'ws-opts' in data:
                    try:
                        v['host'] = data['ws-opts']['headers']['Host']
                    except KeyError: pass
                    if 'path' in data['ws-opts']:
                        v['path'] = data['ws-opts']['path']
            elif v['net'] == 'h2':
                if 'h2-opts' in data:
                    if 'host' in data['h2-opts']:
                        v['host'] = ','.join(data['h2-opts']['host'])
                    if 'path' in data['h2-opts']:
                        v['path'] = data['h2-opts']['path']
            elif v['net'] == 'grpc':
                if 'grpc-opts' in data:
                    if 'grpc-service-name' in data['grpc-opts']:
                        v['path'] = data['grpc-opts']['grpc-service-name']
            if ('tls' in data) and data['tls']:
                v['tls'] = 'tls'
            return 'vmess://'+b64encodes(json.dumps(v, ensure_ascii=False))

        if self.type == 'ss':
            passwd = b64encodes_safe(data['cipher']+':'+data['password'])
            return f"ss://{passwd}@{data['server']}:{data['port']}#{quote(data['name'])}"
        if self.type == 'ssr':
            ret = (':'.join([str(self.data[_]) for _ in ('server','port',
                                        'protocol','cipher','obfs')]) +
                    b64encodes_safe(self.data['password']) +
                    f"remarks={b64encodes_safe(self.data['name'])}")
            for k, urlk in (('obfs-param','obfsparam'), ('protocol-param','protoparam'), ('group','group')):
                if k in self.data:
                    ret += '&'+urlk+'='+b64encodes_safe(self.data[k])
            return "ssr://"+ret

        if self.type == 'trojan':
            passwd = quote(data['password'])
            name = quote(data['name'])
            ret = f"trojan://{passwd}@{data['server']}:{data['port']}?"
            if 'skip-cert-verify' in data:
                ret += f"allowInsecure={int(data['skip-cert-verify'])}&"
            if 'sni' in data:
                ret += f"sni={data['sni']}&"
            if 'alpn' in data:
                if len(data['alpn']) >= 2:
                    ret += "alpn=h2%2Chttp%2F1.1&"
                else:
                    ret += f"alpn={quote(data['alpn'][0])}&"
            if 'network' in data:
                if data['network'] == 'grpc':
                    ret += f"type=grpc&serviceName={data['grpc-opts']['grpc-service-name']}"
                elif data['network'] == 'ws':
                    ret += f"type=ws&"
                    if 'ws-opts' in data:
                        try:
                            ret += f"host={data['ws-opts']['headers']['Host']}&"
                        except KeyError: pass
                        if 'path' in data['ws-opts']:
                            ret += f"path={data['ws-opts']['path']}"
            ret = ret.rstrip('&')+'#'+name
            return ret

        raise UnsupportedType(self.type)

    @property
    def clash_data(self):
        ret = self.data.copy()
        if 'password' in ret and ret['password'].isdigit():
            ret['password'] = '!!str '+ret['password']
        if 'uuid' in ret and len(ret['uuid']) != len(DEFAULT_UUID):
            ret['uuid'] = DEFAULT_UUID
        if 'group' in ret: del ret['group']
        return ret

    def supports_clash(self):
        if 'cipher' not in self.data: return True
        if not self.data['cipher']: return True
        if self.type == 'vless': return False
        elif self.type == 'vmess':
            supported = CLASH_CIPHER_VMESS
        elif self.type == 'ss' or self.type == 'ssr':
            supported = CLASH_CIPHER_SS
        elif self.type == 'trojan': return True
        if self.data['cipher'] not in supported: return False
        if self.type == 'ssr':
            if 'obfs' in self.data and self.data['obfs'] not in CLASH_SSR_OBFS:
                return False
            if 'protocol' in self.data and self.data['protocol'] not in CLASH_SSR_PROTOCOL:
                return False
        if 'plugin-opts' in self.data and 'mode' in self.data['plugin-opts'] \
                and not self.data['plugin-opts']['mode']: return False
        return True

    def supports_ray(self):
        if self.type == 'ss':
            if 'plugin' in self.data and self.data['plugin']: return False
        elif self.type == 'ssr':
            return False
        return True

class Source():
    def __init__(self, url: str) -> None:
        self.url: str = url
        self.content: str = None
        self.sub: list = None
        self.exception: str = None

    def get(self) -> None:
        if self.content: return
        global session
        content = ""
        first_line = True
        tp = None
        try:
            with session.get(self.url, stream=True) as r:
                if r.status_code != 200:
                    self.content = r.status_code
                    return
                for lineb in r.iter_lines():
                    if not lineb: continue
                    line = lineb.decode("utf-8").rstrip().replace('\\r','')
                    if not line: continue
                    if first_line:
                        if ': ' in line:
                            tp = 'yaml'
                        elif '://' in line:
                            tp = 'sub'#raw
                        else: tp = 'sub'
                        first_line = False
                    if tp == 'yaml':
                        if content:
                            if line == "proxy-groups:": break
                            content += line+'\n'
                        elif line == "proxies:":
                            content = line+'\n'
                    elif tp == 'sub':
                        content += line+'\n'
        except KeyboardInterrupt: raise
        except requests.exceptions.RequestException:
            self.content = -1
        except:
            self.content = -2
            self.exception = "在抓取 '"+self.url+"' 时发生错误：\n"+traceback.format_exc()
            threading.Thread(self.print_exc).start()
        else:
            self.content = content
            self.parse()

    def parse(self) -> None:
        try:
            self.sub = parse(self.content)
        except KeyboardInterrupt: raise
        except: self.exception = \
                "在解析 '"+self.url+"' 时发生错误：\n"+traceback.format_exc()

    def print_exc(self) -> None:
        with io_lock:
            print(self.exception, file=sys.stderr, flush=True)

def extract(url: str) -> set:
    global session
    res = session.get(url)
    if res.status_code != 200: return res.status_code
    urls = set()
    for line in res.text:
        if line.startswith("http"):
            urls.add(line)
    return urls

def parse(text) -> list:
    if isinstance(text, str):
        if "proxies:" in text:
            # Clash config
            config = yaml.full_load(text.replace("!<str>","!!str"))
            sub = config['proxies']
        elif '://' in text:
            # V2Ray raw list
            sub = text.strip().split('\n')
        else:
            # V2Ray Sub
            sub = b64decodes(text.strip()).strip().split('\n')
    else: sub = text # 动态节点抓取后直接传入列表
    return sub

merged = set()
unknown = set()
names = set()
def merge(text, parsed=False) -> None:
    global merged, unknown, names
    if parsed: sub = text
    else: sub = parse(text)
    if not sub: print("空订阅，跳过！", end='', flush=True); return
    for p in sub:
        if isinstance(p, str):
            if not p.isascii() or '://' not in p: continue
            ok = True
            for ch in '!|@#`~()[]{} ':
                if ch in p:
                    ok = False; break
            if not ok: continue
        try: n = Node(p)
        except KeyboardInterrupt: raise
        except UnsupportedType as e:
            if len(e.args) == 1:
                print(f"不支持的类型：{e}")
            unknown.add(p)
        except: traceback.print_exc()
        else:
            if n not in merged:
                if len(n.data['name']) > 25:
                    n.data['name'] = n.data['name'][:22]+'...'
                while n.data['name'] in names:
                    n.data['name'] += '_'
                names.add(n.data['name'])
                merged.add(n)

def raw2fastly(url: str) -> str:
    # 由于 Fastly CDN 不好用，因此换成 ghproxy.net，见 README。
    # url = url[34:].split('/')
    # url[1] += '@'+url[2]
    # del url[2]
    # url = "https://fastly.jsdelivr.net/gh/"+('/'.join(url))
    # return url
    if not LOCAL: return url
    if url.startswith("https://raw.githubusercontent.com/"):
        return "https://ghproxy.net/"+url
    return url

if __name__ == '__main__':
    from dynamic import AUTOURLS, AUTOFETCH, set_dynamic_globals
    sources = open("sources.list").read().strip().split('\n')
    session = requests.Session()
    if PROXY:
        session.proxies = {'http': PROXY, 'https': PROXY}
    print("正在生成动态链接...")
    set_dynamic_globals(session, LOCAL)
    for auto_fun in AUTOURLS:
        print("正在生成 '"+auto_fun.__name__+"'...")
        try: url = auto_fun()
        except requests.exceptions.RequestException: pass
        except: traceback.print_exc()
        else:
            if url:
                if isinstance(url, str):
                    sources.append(url)
                elif isinstance(url, (list, tuple, set)):
                    sources.extend(url)
    print("正在整理链接...")
    sources_final = set()
    airports = set()
    for source in sources:
        if not source: continue
        if source[0] == '#': continue
        sub = source
        if sub[0] == '!':
            if LOCAL: continue
            sub = sub[1:]
        if sub[0] == '*':
            isairport = True
            sub = sub[1:]
        else: isairport = False
        if sub[0] == '+':
            tags = sub.split()
            sub = tags.pop()
            while tags:
                tag = tags.pop(0)
                if tag[0] != '+': break
                if tag == '+date':
                    sub = datetime.datetime.now().strftime(sub)
        sub = raw2fastly(sub)
        if isairport: airports.add(sub)
        else: sources_final.add(sub)

    if airports:
        print("正在抓取机场列表...")
        for sub in airports:
            print("合并 '"+sub+"'... ", end='', flush=True)
            try:
                res = extract(sub)
            except KeyboardInterrupt:
                print("正在退出...")
                break
            except requests.exceptions.RequestException:
                print("合并失败！")
            except: traceback.print_exc()
            else:
                if isinstance(res, int):
                    print(res)
                else:
                    for url in res:
                        sources_final.add(url)
                    print("完成！")

    print("正在整理链接...")
    sources_final = list(sources_final)
    sources_final.sort()

    print("开始抓取！")
    sources_obj = [Source(url) for url in sources_final]
    threads = [threading.Thread(target=_.get) for _ in sources_obj]
    for thread in threads: thread.start()
    for i in range(len(sources_obj)):
        with io_lock:
            print("抓取 '"+sources_final[i]+"'... ", end='', flush=True)
            try: threads[i].join()
            except KeyboardInterrupt:
                print("正在退出...")
                break
            res = sources_obj[i].content
            if isinstance(res, int):
                if res < 0: print("抓取失败！")
                else: print(res)
            else:
                print("正在合并... ", end='', flush=True)
                try:
                    merge(sources_obj[i].sub, parsed=True)
                except KeyboardInterrupt:
                    print("正在退出...")
                    break
                except:
                    print("失败！")
                    traceback.print_exc()
                else: print("完成！")
    print("正在抓取动态节点...")
    for auto_fun in AUTOFETCH:
        print("正在抓取 '"+auto_fun.__name__+"'...")
        try: merge(auto_fun())
        except KeyboardInterrupt: print("正在退出...");break
        except: traceback.print_exc()

    print("\n正在写出 V2Ray 订阅...")
    txt = ""
    unsupports = 0
    for p in merged:
        try:
            if p.supports_ray():
                txt += p.url + '\n'
            else: unsupports += 1
        except: traceback.print_exc()
    for p in unknown:
        txt += p+'\n'
    print(f"共有 {len(merged)-unsupports} 个正常节点，{len(unknown)} 个无法解析的节点，共",
            len(merged)+len(unknown),f"个。{unsupports} 个节点不被 V2Ray 支持。")

    with open("list_raw.txt",'w') as f:
        f.write(txt)
    with open("list.txt",'w') as f:
        f.write(b64encodes(txt))
    print("写出完成！")

    with open("config.yml", encoding="utf-8") as f:
        conf = yaml.full_load(f)
    print("正在解析 Adblock 列表...")
    abfurls = (
        "https://raw.githubusercontent.com/AdguardTeam/FiltersRegistry/master/filters/filter_2_Base/filter.txt",
        "https://raw.githubusercontent.com/AdguardTeam/FiltersRegistry/master/filters/filter_224_Chinese/filter.txt",
        "https://raw.githubusercontent.com/AdguardTeam/FiltersRegistry/master/filters/filter_15_DnsFilter/filter.txt",
        "https://malware-filter.gitlab.io/malware-filter/urlhaus-filter-ag.txt"
    )
    blocked = set()
    for url in abfurls:
        url = raw2fastly(url)
        try:
            res = session.get(url)
        except requests.exceptions.RequestException:
            print(url, "下载失败！")
        if res.status_code != 200:
            print(url, res.status_code)
            continue
        for line in res.text.strip().split('\n'):
            line = line.strip()
            if line[:2] == '||' and ('/' not in line) and ('?' not in line) and \
                            (line[-1] == '^' or line.endswith("$all")):
                blocked.add(line.strip('al').strip('|^$'))
    adblock_rules = []
    for domain in blocked:
        adblock_rules.append(f"DOMAIN-SUFFIX,{domain},{conf['proxy-groups'][-1]['name']}")

    print("正在写出 Clash 订阅...")
    rules = conf['rules']
    rules2 = list(set(rules))
    rules2.sort(key=rules.index)
    conf['rules'] = adblock_rules + rules2
    conf['proxies'] = []
    names_clash = set()
    for p in merged:
        if p.supports_clash():
            conf['proxies'].append(p.clash_data)
            names_clash.add(p.data['name'])
    names_clash = list(names_clash)
    for group in conf['proxy-groups']:
        if not group['proxies']:
            group['proxies'] = names_clash
    with open("list.yml", 'w', encoding="utf-8") as f:
        f.write(yaml.dump(conf, allow_unicode=True).replace('!!str ',''))

    print("正在写出统计信息...")
    out = "序号,链接,节点数\n"
    for i, source in enumerate(sources_obj):
        out += f"{i},{source.url},"
        try: out += f"{len(source.sub)}"
        except: out += '0'
        out += '\n'
    out += f"\n总计,,{len(merged)}\n"
    open("list_result.csv",'w').write(out)

    print("写出完成！")
