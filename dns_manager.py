# DNS Manager для Windows: локальный DNS-прокси 127.0.0.1:53 -> ваши серверы (DoT / DoH / UDP) по порядку списка.
import json, os, socket, ssl, struct, subprocess, sys, threading, time, atexit
from concurrent.futures import ThreadPoolExecutor

APP = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "DnsManager")
CFG = os.path.join(APP, "servers.json")
FLAG = os.path.join(APP, "dns_changed.json")
BOOT = ["1.1.1.1", "8.8.8.8"]  # только чтобы узнать IP самих серверов из списка, если системный DNS уже занят нами
PROTOS = ["AUTO", "DoT", "DoH", "UDP"]
DEFAULTS = [
    {"name": "WebShift VPN", "addr": "d.webshiftvpn.ru", "proto": "AUTO", "on": True},
    {"name": "Nulls Proxy", "addr": "dns.nullsproxy.com", "proto": "AUTO", "on": True},
    {"name": "Cloudflare (запасной)", "addr": "1.1.1.1", "proto": "DoT", "on": False},
]
servers = []
_ip = {}


def load():
    try:
        with open(CFG, encoding="utf-8") as f:
            d = json.load(f)
            if isinstance(d, list):
                return d
    except Exception:
        pass
    return [dict(x) for x in DEFAULTS]


def save():
    try:
        os.makedirs(APP, exist_ok=True)
        with open(CFG, "w", encoding="utf-8") as f:
            json.dump(servers, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


# ---------- DNS-утилиты ----------
def is_ip(s):
    for fam in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(fam, s)
            return True
        except OSError:
            pass
    return False


def build_a_query(host):
    q = b"".join(bytes([len(p)]) + p.encode() for p in host.split(".")) + b"\0" + struct.pack(">HH", 1, 1)
    return struct.pack(">HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0) + q


def skip_name(b, i):
    while True:
        n = b[i]
        if n == 0:
            return i + 1
        if n & 0xC0 == 0xC0:
            return i + 2
        i += n + 1


def parse_a(r):
    qd, an = struct.unpack(">HH", r[4:8])
    i = 12
    for _ in range(qd):
        i = skip_name(r, i) + 4
    for _ in range(an):
        i = skip_name(r, i)
        t, _c, _ttl, ln = struct.unpack(">HHIH", r[i:i + 10])
        i += 10
        if t == 1 and ln == 4:
            return socket.inet_ntoa(r[i:i + 4])
        i += ln
    return None


def host_ip(host):
    if is_ip(host):
        return host
    e = _ip.get(host)
    if e and e[1] > time.time():
        return e[0]
    for b in BOOT:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(2)
                s.sendto(build_a_query(host), (b, 53))
                ip = parse_a(s.recvfrom(2048)[0])
            if ip:
                _ip[host] = (ip, time.time() + 600)
                return ip
        except Exception:
            pass
    if e:
        return e[0]
    raise OSError("не удалось узнать IP " + host)


def recvn(s, n):
    b = b""
    while len(b) < n:
        c = s.recv(n - len(b))
        if not c:
            raise OSError("closed")
        b += c
    return b


def ask_dot(host, q):
    ip = host_ip(host)
    ctx = ssl.create_default_context()
    with socket.create_connection((ip, 853), 2.5) as raw:
        raw.settimeout(3)
        with ctx.wrap_socket(raw, server_hostname=host) as s:
            s.sendall(struct.pack(">H", len(q)) + q)
            n = struct.unpack(">H", recvn(s, 2))[0]
            return recvn(s, n)


def dechunk(b):
    out = b""
    while b:
        l, _, b = b.partition(b"\r\n")
        n = int(l.split(b";")[0], 16)
        if n == 0:
            break
        out += b[:n]
        b = b[n + 2:]
    return out


def ask_doh(addr, q):
    host, _, path = addr.partition("/")
    path = "/" + (path or "dns-query")
    ip = host_ip(host)
    ctx = ssl.create_default_context()
    with socket.create_connection((ip, 443), 2.5) as raw:
        raw.settimeout(3)
        with ctx.wrap_socket(raw, server_hostname=host) as s:
            req = ("POST %s HTTP/1.1\r\nHost: %s\r\nContent-Type: application/dns-message\r\n"
                   "Accept: application/dns-message\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
                   % (path, host, len(q))).encode() + q
            s.sendall(req)
            data = b""
            while True:
                try:
                    c = s.recv(65536)
                except OSError:
                    break
                if not c:
                    break
                data += c
    head, _, body = data.partition(b"\r\n\r\n")
    if head.split(b"\r\n")[0].split()[1] != b"200":
        raise OSError("http")
    return dechunk(body) if b"chunked" in head.lower() else body


def ask_udp(host, q):
    ip = host_ip(host)
    fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
    with socket.socket(fam, socket.SOCK_DGRAM) as s:
        s.settimeout(2)
        s.sendto(q, (ip, 53))
        return s.recvfrom(4096)[0]


def ask(addr, proto, q):
    if proto == "DoT":
        return ask_dot(addr.partition("/")[0], q)
    if proto == "DoH":
        return ask_doh(addr, q)
    return ask_udp(addr.partition("/")[0], q)


class Engine:
    PORT = 53

    def __init__(self):
        self.cache, self.learned, self.last = {}, {}, "—"
        self.socks, self.running, self.pool = [], False, None

    def resolve(self, q):
        if len(q) < 12:
            return q
        key = q[2:]
        c = self.cache.get(key)
        if c and c[1] > time.time():
            return q[:2] + c[0][2:]
        for s in [x for x in list(servers) if x.get("on")]:
            if s["proto"] == "AUTO":
                order = list(dict.fromkeys(([self.learned[s["addr"]]] if s["addr"] in self.learned else []) + ["DoT", "DoH", "UDP"]))
            else:
                order = [s["proto"]]
            for pr in order:
                try:
                    r = ask(s["addr"], pr, q)
                    if len(r) >= 12:
                        self.last = "%s · %s" % (s["name"], pr)
                        if s["proto"] == "AUTO":
                            self.learned[s["addr"]] = pr
                        if r[3] & 15 == 0:
                            if len(self.cache) > 2000:
                                self.cache.clear()
                            self.cache[key] = (r, time.time() + 60)
                        return r
                except Exception:
                    pass
        self.last = "нет ответа ни от одного сервера"
        r = bytearray(q)
        r[2] = 0x80 | (q[2] & 1)
        r[3] = 0x82
        r[6:12] = bytes(6)
        return bytes(r)

    def handle(self, s, data, addr):
        try:
            s.sendto(self.resolve(data), addr)
        except Exception:
            pass

    def serve(self, s):
        while self.running:
            try:
                data, addr = s.recvfrom(4096)
            except OSError:
                if not self.running:
                    break
                continue
            self.pool.submit(self.handle, s, data, addr)

    def start(self):
        s4 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s4.bind(("127.0.0.1", self.PORT))
        except OSError:
            s4.close()
            raise
        self.socks = [s4]
        try:
            s6 = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
            s6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            s6.bind(("::1", self.PORT))
            self.socks.append(s6)
        except OSError:
            pass
        self.running = True
        self.pool = ThreadPoolExecutor(64)
        for s in self.socks:
            threading.Thread(target=self.serve, args=(s,), daemon=True).start()

    def stop(self):
        self.running = False
        for s in self.socks:
            try:
                s.close()
            except OSError:
                pass
        self.socks = []
        if self.pool:
            self.pool.shutdown(wait=False, cancel_futures=True)


# ---------- системный DNS Windows ----------
def ps(cmd):
    r = subprocess.run(["powershell", "-NoProfile", "-Command", "$ErrorActionPreference='Stop';" + cmd],
                       capture_output=True, text=True, creationflags=0x08000000)
    if r.returncode != 0:
        raise OSError((r.stderr or r.stdout).strip() or "powershell")
    return r.stdout


def set_sys_dns():
    idx = [int(x) for x in ps("(Get-NetAdapter | Where-Object Status -eq 'Up').ifIndex").split() if x.isdigit()]
    if not idx:
        raise OSError("нет активных сетевых адаптеров")
    os.makedirs(APP, exist_ok=True)
    with open(FLAG, "w") as f:
        json.dump(idx, f)
    for i in idx:
        ps("Set-DnsClientServerAddress -InterfaceIndex %d -ServerAddresses ('127.0.0.1','::1')" % i)
    ps("Clear-DnsClientCache")


def restore_sys_dns():
    try:
        with open(FLAG) as f:
            idx = json.load(f)
    except Exception:
        return
    for i in idx:
        try:
            ps("Set-DnsClientServerAddress -InterfaceIndex %d -ResetServerAddresses" % i)
        except Exception:
            pass
    try:
        ps("Clear-DnsClientCache")
    except Exception:
        pass
    try:
        os.remove(FLAG)
    except OSError:
        pass


# ---------- интерфейс ----------
def main():
    import tkinter as tk
    from tkinter import ttk, messagebox

    eng = Engine()
    servers[:] = load()
    root = tk.Tk()
    root.title("DNS Manager")
    root.geometry("660x560")

    top = ttk.Frame(root, padding=10)
    top.pack(fill="x")
    btn = ttk.Button(top, text="Запустить")
    btn.pack(side="left")
    st = ttk.Label(top, text="Выключено")
    st.pack(side="left", padx=10)

    tv = ttk.Treeview(root, columns=("on", "name", "addr", "proto"), show="headings", height=10, selectmode="browse")
    for c, t, w in (("on", "Вкл", 50), ("name", "Название", 170), ("addr", "Адрес", 260), ("proto", "Протокол", 80)):
        tv.heading(c, text=t)
        tv.column(c, width=w, anchor="w")
    tv.pack(fill="both", expand=True, padx=10)

    def refresh(sel=None):
        tv.delete(*tv.get_children())
        for i, s in enumerate(servers):
            tv.insert("", "end", iid=str(i), values=("✔" if s["on"] else "—", "%d. %s" % (i + 1, s["name"]), s["addr"], s["proto"]))
        if sel is not None and 0 <= sel < len(servers):
            tv.selection_set(str(sel))

    def cur():
        s = tv.selection()
        return int(s[0]) if s else None

    def toggle_sel(_e=None):
        i = cur()
        if i is not None:
            servers[i]["on"] = not servers[i]["on"]
            save()
            refresh(i)

    def move(d):
        i = cur()
        if i is None or not 0 <= i + d < len(servers):
            return
        servers[i], servers[i + d] = servers[i + d], servers[i]
        save()
        refresh(i + d)

    def delete():
        i = cur()
        if i is not None and messagebox.askyesno("Удалить", "Удалить «%s»?" % servers[i]["name"]):
            del servers[i]
            save()
            refresh()

    tv.bind("<Double-1>", toggle_sel)
    bar = ttk.Frame(root, padding=(10, 8))
    bar.pack(fill="x")
    for t, f in (("Вкл/выкл", toggle_sel), ("Выше", lambda: move(-1)), ("Ниже", lambda: move(1)), ("Удалить", delete)):
        ttk.Button(bar, text=t, command=f).pack(side="left", padx=2)

    form = ttk.LabelFrame(root, text="Добавить сервер", padding=10)
    form.pack(fill="x", padx=10, pady=4)
    en = ttk.Entry(form, width=18)
    ea = ttk.Entry(form, width=28)
    ep = ttk.Combobox(form, values=PROTOS, width=6, state="readonly")
    ep.set("AUTO")
    for w, t in ((en, "Название"), (ea, "Адрес (IP или домен)"), (ep, "Протокол")):
        fr = ttk.Frame(form)
        fr.pack(side="left", padx=4)
        ttk.Label(fr, text=t).pack(anchor="w")
        w.pack()

    def add():
        a = ea.get().strip()
        if not a or any(ch.isspace() for ch in a):
            messagebox.showwarning("Адрес", "Введите адрес без пробелов")
            return
        servers.append({"name": en.get().strip() or a, "addr": a, "proto": ep.get(), "on": True})
        save()
        en.delete(0, "end")
        ea.delete(0, "end")
        refresh(len(servers) - 1)

    ttk.Button(form, text="Добавить", command=add).pack(side="left", padx=6, pady=(14, 0))
    ttk.Label(root, wraplength=620, foreground="#666", padding=10,
              text="Серверы опрашиваются сверху вниз. При «Запустить» системный DNS на активных адаптерах "
                   "меняется на 127.0.0.1, при «Остановить» возвращается к автоматическому (DHCP).").pack(fill="x")

    def start():
        if not any(s["on"] for s in servers):
            messagebox.showwarning("Серверы", "Включите хотя бы один сервер")
            return
        for s in servers:  # пока системный DNS ещё обычный, запоминаем IP серверов
            h = s["addr"].partition("/")[0]
            if s["on"] and not is_ip(h):
                try:
                    _ip[h] = (socket.gethostbyname(h), time.time() + 600)
                except OSError:
                    pass
        try:
            eng.start()
        except OSError as e:
            messagebox.showerror("Порт 53", "Не удалось занять 127.0.0.1:53 (%s).\nВозможно, порт занят другой программой, "
                                 "или нужны права администратора." % e)
            return
        try:
            set_sys_dns()
        except Exception as e:
            eng.stop()
            restore_sys_dns()
            messagebox.showerror("Системный DNS", "Не удалось сменить DNS (нужны права администратора):\n%s" % e)

    def stop():
        restore_sys_dns()
        eng.stop()

    btn.config(command=lambda: stop() if eng.running else start())

    def tick():
        btn.config(text="Остановить" if eng.running else "Запустить")
        st.config(text=("Работает. Последний ответ: " + eng.last) if eng.running else "Выключено")
        root.after(1000, tick)

    def close():
        if eng.running:
            stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    atexit.register(restore_sys_dns)
    restore_sys_dns()  # если прошлый запуск завершился аварийно
    refresh()
    tick()
    root.mainloop()


if __name__ == "__main__":
    if "--restore" in sys.argv:
        restore_sys_dns()
    else:
        main()
