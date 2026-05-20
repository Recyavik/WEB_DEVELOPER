"""
esp_config.py — настройка ESP32 (1Т REX) по USB-Serial.

Разовая утилита для прошивки в EEPROM ESP32 параметров WiFi и адреса
сервера. После настройки робот при включении сам подключится к указанной
WiFi-сети и откроет WebSocket к bridge.py на указанном IP:порт.

Зависимости (один раз):
    pip install pyserial

Использование (мини-GUI на tkinter):
    python esp_config.py

Поля:
    COM-порт      — порт, к которому подключён ESP32 по USB
    Имя WiFi      — SSID сети (например, имя телефона-точки доступа)
    Пароль WiFi   — пароль сети
    Имя устройства — произвольное имя робота (для логов)
    IP сервера    — адрес ПК, на котором крутится bridge.py
    Порт сервера  — порт bridge.py для ESP32 (по умолчанию 8080)

Кнопки:
    «Прочитать»  — забрать текущие настройки из ESP32 (GW/GP/GN)
    «Записать»   — отправить и сохранить SAVE
    «Очистить»   — стереть EEPROM (SZEROS)
    «Сканировать порты» — показать доступные COM-порты на этом ПК

Командный словарь ESP32 (по протоколу прошивки 1Т REX):
    GW / GP / GN          — получить SSID / пароль / имя
    SW:val / SP:val /
    SN:val / SIP:val /
    SPORT:val             — установить
    SAVE                  — записать в EEPROM
    SZEROS                — стереть EEPROM
"""
import socket
import sys
import time
import tkinter as tk
from tkinter import ttk, messagebox
from tkinter.scrolledtext import ScrolledText

try:
    import serial
    import serial.tools.list_ports as list_ports
except ImportError:
    print("Нужен pyserial: pip install pyserial")
    sys.exit(1)


BAUD = 38400
TIMEOUT = 2.0


def list_local_ips():
    """Найти все IPv4-адреса этого ПК (не loopback) — чтобы знать,
    какой IP вписать в SIP (адрес ПК с bridge.py)."""
    ips = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            if info[0] == socket.AF_INET:
                ip = info[4][0]
                if ip and not ip.startswith("127."):
                    ips.append(ip)
    except Exception:
        pass
    # Запасной способ — попытка коннекта на внешний адрес даёт текущий
    # «исходящий» IP (без реального коннекта).
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return sorted(set(ips))


def run_serial_commands(port: str, commands: list, on_log, on_value=None):
    """Открывает COM-порт, шлёт команды по очереди.
    `commands` — список (command_str, optional_key_для_on_value).
    on_log(level, msg)        — куда писать лог
    on_value(key, value)      — если в ответе есть «key: value», вызвать
    """
    try:
        with serial.Serial(port, BAUD, timeout=TIMEOUT) as ser:
            on_log("system", f"Открыт порт {port} (baud {BAUD}).")
            # Подождать пока ESP32 проснётся после reset на открытии порта.
            time.sleep(2.0)
            for cmd, key in commands:
                on_log("send", f"→ {cmd}")
                ser.write(f"{cmd}\n".encode())
                ser.flush()
                resp = ser.readline().decode(errors="ignore").strip()
                if resp:
                    on_log("recv", f"← {resp}")
                    if key and on_value and ":" in resp:
                        _, _, val = resp.partition(":")
                        on_value(key, val.strip())
                else:
                    on_log("warn", "← (пустой ответ)")
            on_log("system", "Готово.")
    except serial.SerialException as e:
        on_log("error", f"Ошибка порта: {e}")
        messagebox.showerror("Ошибка", f"Не открыть {port}: {e}")
    except Exception as e:
        on_log("error", f"Сбой: {e}")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ESP32 Config (1Т REX) — esp_config.py")
        self.resizable(False, False)
        self._build_ui()
        self.refresh_ports()
        self.refresh_ips()

    def _build_ui(self):
        frm = ttk.Frame(self, padding=10)
        frm.grid(row=0, column=0, sticky="news")
        frm.columnconfigure(1, weight=1)

        # COM-порт + кнопка обновить
        ttk.Label(frm, text="COM-порт:").grid(row=0, column=0, sticky="w", pady=3)
        self.cb_port = ttk.Combobox(frm, state="readonly", width=30)
        self.cb_port.grid(row=0, column=1, sticky="ew")
        ttk.Button(frm, text="↻", width=3, command=self.refresh_ports
                   ).grid(row=0, column=2, padx=4)

        # WiFi-настройки
        ttk.Label(frm, text="Имя WiFi (SSID):").grid(row=1, column=0, sticky="w", pady=3)
        self.var_ssid = tk.StringVar()
        ttk.Entry(frm, textvariable=self.var_ssid).grid(row=1, column=1, columnspan=2, sticky="ew")

        ttk.Label(frm, text="Пароль WiFi:").grid(row=2, column=0, sticky="w", pady=3)
        self.var_pwd = tk.StringVar()
        ttk.Entry(frm, textvariable=self.var_pwd, show="•").grid(
            row=2, column=1, columnspan=2, sticky="ew")

        ttk.Label(frm, text="Имя устройства:").grid(row=3, column=0, sticky="w", pady=3)
        self.var_name = tk.StringVar()
        ttk.Entry(frm, textvariable=self.var_name).grid(row=3, column=1, columnspan=2, sticky="ew")

        # IP сервера (bridge.py) + детект
        ttk.Label(frm, text="IP сервера (bridge.py):").grid(row=4, column=0, sticky="w", pady=3)
        self.cb_ip = ttk.Combobox(frm, width=30)
        self.cb_ip.grid(row=4, column=1, sticky="ew")
        ttk.Button(frm, text="↻", width=3, command=self.refresh_ips
                   ).grid(row=4, column=2, padx=4)

        ttk.Label(frm, text="Порт сервера:").grid(row=5, column=0, sticky="w", pady=3)
        self.var_port = tk.StringVar(value="8080")
        ttk.Entry(frm, textvariable=self.var_port).grid(row=5, column=1, columnspan=2, sticky="ew")

        # Кнопки
        btn_frm = ttk.Frame(frm)
        btn_frm.grid(row=6, column=0, columnspan=3, pady=(8, 4))
        ttk.Button(btn_frm, text="📥 Прочитать", command=self.do_read
                   ).pack(side="left", padx=4)
        ttk.Button(btn_frm, text="📤 Записать", command=self.do_write
                   ).pack(side="left", padx=4)
        ttk.Button(btn_frm, text="✕ Очистить EEPROM", command=self.do_clear
                   ).pack(side="left", padx=4)

        # Лог
        ttk.Label(frm, text="Лог:").grid(row=7, column=0, sticky="nw", pady=(8, 0))
        self.log = ScrolledText(frm, width=70, height=14, state="disabled",
                                font=("Consolas", 9))
        self.log.grid(row=8, column=0, columnspan=3, sticky="news", pady=4)
        self.log.tag_config("system", foreground="#888")
        self.log.tag_config("send", foreground="#06c")
        self.log.tag_config("recv", foreground="#080")
        self.log.tag_config("warn", foreground="#c80")
        self.log.tag_config("error", foreground="#c00")

    # ── Действия ─────────────────────────────────────────────────────
    def refresh_ports(self):
        ports = [p.device for p in list_ports.comports()]
        if not ports:
            self.cb_port["values"] = ["Порты не найдены"]
            self.cb_port.set("Порты не найдены")
        else:
            self.cb_port["values"] = ports
            self.cb_port.set(ports[0])

    def refresh_ips(self):
        ips = list_local_ips() or ["127.0.0.1"]
        self.cb_ip["values"] = ips
        if not self.cb_ip.get() or self.cb_ip.get() not in ips:
            self.cb_ip.set(ips[0])

    def get_port(self):
        port = self.cb_port.get()
        if not port or "не найден" in port.lower():
            messagebox.showerror("Ошибка", "Выберите COM-порт.")
            return None
        return port

    def log_msg(self, level, msg):
        self.log.configure(state="normal")
        self.log.insert("end", f"{msg}\n", level)
        self.log.configure(state="disabled")
        self.log.see("end")
        self.update_idletasks()

    def on_value(self, key, value):
        if key == "ssid":  self.var_ssid.set(value)
        elif key == "pwd": self.var_pwd.set(value)
        elif key == "name": self.var_name.set(value)

    def do_read(self):
        port = self.get_port()
        if not port: return
        commands = [("GW", "ssid"), ("GP", "pwd"), ("GN", "name")]
        run_serial_commands(port, commands, self.log_msg, self.on_value)

    def do_write(self):
        port = self.get_port()
        if not port: return
        commands = [
            (f"SW:{self.var_ssid.get()}",  None),
            (f"SP:{self.var_pwd.get()}",   None),
            (f"SN:{self.var_name.get()}",  None),
            (f"SIP:{self.cb_ip.get()}",    None),
            (f"SPORT:{self.var_port.get()}", None),
            ("SAVE", None),
        ]
        run_serial_commands(port, commands, self.log_msg)

    def do_clear(self):
        port = self.get_port()
        if not port: return
        if not messagebox.askyesno("Подтверждение",
                                    "Полностью стереть EEPROM ESP32?\n"
                                    "Робот забудет SSID/пароль/IP — после "
                                    "этого его нужно настроить заново."):
            return
        run_serial_commands(port, [("SZEROS", None)], self.log_msg)


if __name__ == "__main__":
    app = App()
    app.mainloop()
