import asyncio
import json
import socket
import sys
import threading
import queue
import time

# --- GUI ---
import tkinter as tk
from tkinter import ttk, messagebox
from tkinter.scrolledtext import ScrolledText

# --- Networking & Serial ---
import websockets
import serial
import serial.tools.list_ports
import serial_asyncio

# --- FIX: Platform-Specific Import for IP Discovery ---
# Only import netifaces on Linux or macOS, as it's most reliable there.
if sys.platform in ('linux', 'darwin'):
    try:
        import netifaces
    except ImportError:
        print("Warning: 'netifaces' module not found. IP detection may be limited on this platform.")
        print("Install it with: pip install netifaces")
        netifaces = None
else:
    # On Windows, we don't need it.
    netifaces = None

# --- App Information ---
APP_NAME = "Car Server"
APP_VERSION = "1.7.2" # Version for cross-platform IP fix
BAUDRATE = 38400

# The ServerBackend and most of the App class remain unchanged. The only change
# is the call to the new get_all_local_ips() function.
class ServerBackend:
    # ... (This class is identical to the previous version, no changes needed) ...
    """Handles all the async server logic (WebSockets, Serial) in a separate thread."""
    def __init__(self, config, gui_queue):
        self.config = config
        self.gui_queue = gui_queue
        self.loop = None
        self.browser_client, self.esp32_client = None, None
        self.serial_writer, self.serial_reader = None, None
        self.pending_serial_commands = {} 
        self.is_running = True
        self.browser_ws_server = None
        self.esp32_ws_server = None

    def _log(self, level: str, message: str): self.gui_queue.put(("log", level, message))
    def _update_status(self, component: str, status: bool): self.gui_queue.put(("status", component, status))

    async def _browser_handler(self, websocket):
        if self.browser_client: await self.browser_client.close()
        self.browser_client = websocket
        self._log("wss", f"Клиент браузера подключен с адреса: {websocket.remote_address}")
        self._update_status("browser", True)
        try:
            async for message in websocket:
                try:
                    data = json.loads(message)
                    command_id, command_str = data.get("id"), data.get("command")
                    if data.get("usb") and str(data.get("usb")).lower() == 'true':
                        if self.serial_writer:
                            self._log("bridge", f"Браузер -> Машинка (Serial): {command_str}")
                            self.pending_serial_commands[command_id] = True
                            self.serial_writer.write(f"{command_str}\n".encode())
                            await self.serial_writer.drain()
                        else: self._log("error", "Не могу отправить: последовательный порт не подключен.")
                    else:
                        if self.esp32_client:
                            self._log("bridge", f"Браузер -> Машинка (WS): {message}")
                            await self.esp32_client.send(message)
                        else: self._log("error", "Не могу отправить: WebSocket клиент не подключен.")
                except Exception as e: self._log("error", f"Ошибка обработки сообщения с браузера: {e}")
        finally: self.browser_client = None; self._update_status("browser", False)

    async def _esp32_handler(self, websocket):
        if self.esp32_client: await self.esp32_client.close()
        self.esp32_client = websocket
        self._log("ws", f"Клиент машинки подключен по адресу: {websocket.remote_address}")
        self._update_status("esp32", True)
        try:
            async for message in websocket:
                self._log("bridge", f"Машинка (WS) -> Браузер: {message}")
                if self.browser_client: await self.browser_client.send(message)
        finally: self.esp32_client = None; self._update_status("esp32", False)

    async def _read_from_serial(self):
        self._log("serial", "Чтение порта запущено.")
        while self.is_running:
            try:
                line = await self.serial_reader.readline()
                response_data = line.decode('utf-8', errors='ignore').strip()
                self.gui_queue.put(("log", "raw_serial", response_data))
                if not response_data: continue
                if self.pending_serial_commands:
                    command_id = next(iter(self.pending_serial_commands))
                    del self.pending_serial_commands[command_id]
                    self._log("serial", f"Машинка -> Serial: {response_data}")
                    response_json = json.dumps({"id": command_id, "value": response_data})
                    if self.browser_client: await self.browser_client.send(response_json)
            except asyncio.CancelledError: break
            except Exception: self._log("error", "Последовательный порт отключен."), self._update_status("serial", False); break

    async def start_servers(self):
        self.loop = asyncio.get_running_loop()
        local_ip = self.config.get('SERVER_IP', '0.0.0.0')
        self._log("system", f"Пробую открыть серверы на IP: {local_ip}")
        if self.config.get('SERIAL_PORT_PATH'):
            try:
                self.serial_reader, self.serial_writer = await serial_asyncio.open_serial_connection(url=self.config['SERIAL_PORT_PATH'], baudrate=self.config['BAUD_RATE'])
                self._log("serial", f"Порт {self.config['SERIAL_PORT_PATH']} открыт (скорость: {BAUDRATE}).")
                self._update_status("serial", True)
                self.loop.create_task(self._read_from_serial())
            except Exception as e: self._log("error", f"Не могу открыть порт: {e}"); self._update_status("serial", False)
        else:
            self._log("system", "Запуск в режиме 'только WebSocket'.")
        try:
            self.browser_ws_server = await websockets.serve(self._browser_handler, "0.0.0.0", self.config['BROWSER_WS_PORT'])
            self._log("wss", f"Адрес для браузера - ws://{local_ip}:{self.config['BROWSER_WS_PORT']}")
            self.esp32_ws_server = await websockets.serve(self._esp32_handler, "0.0.0.0", self.config['ESP32_WS_PORT'])
            self._log("ws", f"Адрес для машинки - ws://{local_ip}:{self.config['ESP32_WS_PORT']}")
            await asyncio.gather(self.browser_ws_server.wait_closed(), self.esp32_ws_server.wait_closed())
        except OSError as e: self._log("error", f"Не могу запустить WebSocket сервер: {e} (порт занят?)"); self.gui_queue.put(("server_error",))
        except Exception as e: self._log("error", f"Возникла непредвиденная ошибка: {e}"); self.gui_queue.put(("server_error",))
        finally: self._log("system", "Серверный цикл завершен.")

    async def shutdown(self):
        self._log("system", "Начинаю процедуру отключения...")
        self.is_running = False
        if self.browser_client: await self.browser_client.close()
        if self.esp32_client: await self.esp32_client.close()
        if self.browser_ws_server: self.browser_ws_server.close()
        if self.esp32_ws_server: self.esp32_ws_server.close()
        await asyncio.sleep(0.1)
        if self.loop: self.loop.stop()

    def stop(self):
        if self.loop and self.loop.is_running(): asyncio.run_coroutine_threadsafe(self.shutdown(), self.loop)

class App(tk.Tk):
    # ... (Most of the App class is identical to the previous version) ...
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} v{APP_VERSION}")
        self.resizable(False, False)
        self.gui_queue = queue.Queue()
        self.backend_thread = None
        self.backend = None
        self._setup_ui()
        self.process_gui_queue()
        self.populate_serial_ports()
        self._populate_ip_addresses()

    def _setup_ui(self):
        main_frame = ttk.Frame(self, padding="10")
        main_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        main_frame.columnconfigure(0, weight=1)

        controls_frame = ttk.LabelFrame(main_frame, text="Настройки соединения", padding="10")
        controls_frame.grid(row=0, column=0, sticky=(tk.W, tk.E), pady=5)
        controls_frame.columnconfigure(1, weight=1)
        ttk.Label(controls_frame, text="Последовательный порт машинки:").grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)
        self.serial_ports_combobox = ttk.Combobox(controls_frame, state="readonly", width=40)
        self.serial_ports_combobox.grid(row=0, column=1, padx=5, sticky=(tk.W, tk.E))
        self.refresh_button = ttk.Button(controls_frame, text="\u21BB", width=3, command=self.populate_serial_ports)
        self.refresh_button.grid(row=0, column=2, padx=5)
        ttk.Label(controls_frame, text="IP-адрес компьютера:").grid(row=1, column=0, padx=5, pady=5, sticky=tk.W)
        self.ip_combobox = ttk.Combobox(controls_frame, state="readonly")
        self.ip_combobox.grid(row=1, column=1, columnspan=2, padx=5, sticky=(tk.W, tk.E))
        ttk.Label(controls_frame, text="Порт WebSocket сайта:").grid(row=2, column=0, padx=5, pady=5, sticky=tk.W)
        self.browser_port_var = tk.StringVar(value="41235")
        self.browser_port_entry = ttk.Entry(controls_frame, textvariable=self.browser_port_var)
        self.browser_port_entry.grid(row=2, column=1, columnspan=2, padx=5, sticky=(tk.W, tk.E))
        ttk.Label(controls_frame, text="Порт WebSocket машинки:").grid(row=3, column=0, padx=5, pady=5, sticky=tk.W)
        self.esp32_port_var = tk.StringVar(value="8080")
        self.esp32_port_entry = ttk.Entry(controls_frame, textvariable=self.esp32_port_var)
        self.esp32_port_entry.grid(row=3, column=1, columnspan=2, padx=5, sticky=(tk.W, tk.E))

        self.esp_config_frame = ttk.LabelFrame(main_frame, text="Настройка ESP32 по Serial", padding="10")
        self.esp_config_frame.grid(row=1, column=0, sticky=(tk.W, tk.E), pady=5)
        self.esp_config_frame.columnconfigure(1, weight=1)
        ttk.Label(self.esp_config_frame, text="Имя WiFi (SSID):").grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)
        self.esp_ssid_var = tk.StringVar()
        self.esp_ssid_entry = ttk.Entry(self.esp_config_frame, textvariable=self.esp_ssid_var)
        self.esp_ssid_entry.grid(row=0, column=1, padx=5, sticky=(tk.W, tk.E))
        ttk.Label(self.esp_config_frame, text="Пароль WiFi:").grid(row=1, column=0, padx=5, pady=5, sticky=tk.W)
        self.esp_password_var = tk.StringVar()
        self.esp_password_entry = ttk.Entry(self.esp_config_frame, textvariable=self.esp_password_var)
        self.esp_password_entry.grid(row=1, column=1, padx=5, sticky=(tk.W, tk.E))
        ttk.Label(self.esp_config_frame, text="Имя устройства:").grid(row=2, column=0, padx=5, pady=5, sticky=tk.W)
        self.esp_name_var = tk.StringVar()
        self.esp_name_entry = ttk.Entry(self.esp_config_frame, textvariable=self.esp_name_var)
        self.esp_name_entry.grid(row=2, column=1, padx=5, sticky=(tk.W, tk.E))
        esp_buttons_frame = ttk.Frame(self.esp_config_frame)
        esp_buttons_frame.grid(row=3, column=0, columnspan=2, pady=10)
        self.esp_get_button = ttk.Button(esp_buttons_frame, text="Получить", command=self.get_esp_settings)
        self.esp_get_button.pack(side=tk.LEFT, padx=5)
        self.esp_send_button = ttk.Button(esp_buttons_frame, text="Отправить", command=self.send_esp_settings)
        self.esp_send_button.pack(side=tk.LEFT, padx=5)
        self.esp_clear_button = ttk.Button(esp_buttons_frame, text="Очистить EEPROM", command=self.clear_esp_eeprom)
        self.esp_clear_button.pack(side=tk.LEFT, padx=5)

        action_frame = ttk.Frame(main_frame)
        action_frame.grid(row=2, column=0, sticky=(tk.W, tk.E), pady=5)
        self.connect_button = ttk.Button(action_frame, text="Соединить", command=self.toggle_connection, width=15)
        self.connect_button.pack(side=tk.LEFT, padx=(0, 20))
        self.debug_toggle_button = ttk.Button(action_frame, text="Отладка", command=self.toggle_debug_frame)
        self.debug_toggle_button.pack(side=tk.LEFT, padx=5)
        status_frame = ttk.Frame(action_frame)
        status_frame.pack(side=tk.RIGHT)
        self.status_labels = {}
        for name, text in [("serial", "Посл. Порт"), ("browser", "Браузер"), ("esp32", "Машинка")]:
            frame = ttk.Frame(status_frame)
            frame.pack(side=tk.LEFT, padx=10)
            ttk.Label(frame, text=f"{text}:").pack(side=tk.LEFT)
            self.status_labels[name] = ttk.Label(frame, text="Отключено", foreground="grey")
            self.status_labels[name].pack(side=tk.LEFT)
        
        self.debug_frame = ttk.LabelFrame(main_frame, text="Отладка Serial", padding=10)
        self.debug_frame.grid(row=3, column=0, sticky="ew", pady=5)
        self.debug_frame.columnconfigure(0, weight=1)
        self.show_raw_serial_var = tk.BooleanVar(value=False)
        self.debug_check = ttk.Checkbutton(self.debug_frame, text="Показать сырые данные", variable=self.show_raw_serial_var)
        self.debug_check.grid(row=0, column=0, columnspan=2, sticky='w')
        self.custom_command_var = tk.StringVar()
        self.custom_command_entry = ttk.Entry(self.debug_frame, textvariable=self.custom_command_var)
        self.custom_command_entry.grid(row=1, column=0, sticky='ew', pady=5)
        self.custom_command_button = ttk.Button(self.debug_frame, text="Отправить", command=self.send_custom_serial_command)
        self.custom_command_button.grid(row=1, column=1, sticky='e', padx=(5,0))
        self.debug_frame.grid_remove()

        log_frame = ttk.LabelFrame(main_frame, text="Логи", padding="5")
        log_frame.grid(row=4, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        main_frame.rowconfigure(4, weight=1)
        self.log_widget = ScrolledText(log_frame, state='disabled', wrap=tk.WORD, font=("Consolas", 9), width=100, height=15)
        self.log_widget.pack(expand=True, fill='both')
        self._setup_log_colors()

        self.server_controls = [self.serial_ports_combobox, self.refresh_button, self.ip_combobox, self.browser_port_entry, self.esp32_port_entry]
        self.esp_config_controls = [self.esp_ssid_entry, self.esp_password_entry, self.esp_name_entry, self.esp_get_button, self.esp_send_button, self.esp_clear_button]
        self.debug_controls = [self.debug_check, self.custom_command_entry, self.custom_command_button]
        self.serial_dependent_controls = self.esp_config_controls + self.debug_controls + [self.debug_toggle_button]
        
    def populate_serial_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        has_ports = bool(ports)
        if not has_ports: self.serial_ports_combobox['values'] = ["Не найдены порты"]; self.serial_ports_combobox.set("Не найдены порты")
        else: self.serial_ports_combobox['values'] = ports; self.serial_ports_combobox.current(0)
        self.set_serial_dependent_controls_state(tk.NORMAL if has_ports else tk.DISABLED)

    def set_serial_dependent_controls_state(self, state):
        if self.backend_thread and self.backend_thread.is_alive(): return
        for widget in self.serial_dependent_controls:
            widget.config(state=state)

    def _populate_ip_addresses(self):
        ips = get_all_local_ips()
        if not ips: ips = ["127.0.0.1"]
        self.ip_combobox['values'] = ips; self.ip_combobox.set(ips[0])

    def toggle_connection(self):
        if self.backend_thread and self.backend_thread.is_alive(): self.disconnect_server()
        else: self.connect_server()

    def connect_server(self):
        self.connect_button.config(state=tk.DISABLED)
        serial_port = self.serial_ports_combobox.get()
        if "Не найдены" in serial_port: serial_port = None
        try:
            browser_port, esp32_port = int(self.browser_port_var.get()), int(self.esp32_port_var.get())
            if not (0 < browser_port < 65536 and 0 < esp32_port < 65536): raise ValueError()
        except ValueError: messagebox.showerror("Ошибка", "Введите корректные порты."); self.connect_button.config(state=tk.NORMAL); return
        config = { "BROWSER_WS_PORT": browser_port, "ESP32_WS_PORT": esp32_port, "SERIAL_PORT_PATH": serial_port, "BAUD_RATE": BAUDRATE, "SERVER_IP": self.ip_combobox.get() }
        self.set_all_controls_state(tk.DISABLED)
        self.connect_button.config(text="Отключить"); self.connect_button.config(state=tk.NORMAL)
        self.backend = ServerBackend(config, self.gui_queue)
        self.backend_thread = threading.Thread(target=asyncio.run, args=(self.backend.start_servers(),), daemon=True)
        self.backend_thread.start()
        self.add_log_message("system", "Запускаю сервер...")

    def disconnect_server(self):
        self.connect_button.config(state=tk.DISABLED)
        if self.backend: self.backend.stop()
        if self.backend_thread and self.backend_thread.is_alive(): self.backend_thread.join(timeout=2.0)
        self.backend, self.backend_thread = None, None
        self.add_log_message("system", "Сервер остановлен.")
        self.set_all_controls_state(tk.NORMAL)
        self.connect_button.config(text="Соединить"); self.connect_button.config(state=tk.NORMAL)
        for name in self.status_labels: self.update_status_indicator(name, None)
        self.populate_serial_ports()

    def set_all_controls_state(self, state):
        all_controls = self.server_controls + self.serial_dependent_controls
        for widget in all_controls:
            if isinstance(widget, ttk.Combobox): widget.config(state="readonly" if state == tk.NORMAL else "disabled")
            else: widget.config(state=state)

    def _task_run_serial_commands(self, port, commands_to_run, get_responses=False):
        self.gui_queue.put(("log", "system", f"Открываю порт {port} (скорость: {BAUDRATE})..."))
        try:
            with serial.Serial(port, BAUDRATE, timeout=2) as ser:
                time.sleep(2)
                for command, response_key in commands_to_run:
                    self.gui_queue.put(("log", "serial", f"Отправка: {command}"))
                    ser.write(f"{command}\n".encode())
                    ser.flush()
                    response = ser.readline().decode(errors='ignore').strip()
                    self.gui_queue.put(("log", "raw_serial", response))
                    if get_responses:
                        if ':' in response:
                            try:
                                key, value = response.split(':', 1); value = value.strip()
                                self.gui_queue.put(("update_esp_field", response_key, value))
                                self.gui_queue.put(("log", "serial", f"Получено для '{response_key}': {value}"))
                            except (ValueError, IndexError): self.gui_queue.put(("log", "error", f"Не удалось распарсить: {response}"))
                        else: self.gui_queue.put(("log", "error", f"Неожиданный ответ: {response}"))
                    elif response: self.gui_queue.put(("log", "serial", f"Ответ устройства: {response}"))
            self.gui_queue.put(("log", "system", "Задача завершена. Порт закрыт."))
        except serial.SerialException as e:
            self.gui_queue.put(("log", "error", f"Ошибка порта: {e}"))
            self.gui_queue.put(("messagebox", "error", f"Не удалось открыть порт {port}."))

    def get_esp_settings(self):
        port = self.serial_ports_combobox.get()
        if not port or "Не найдены" in port: messagebox.showerror("Ошибка", "Выберите порт."); return
        commands = [("GW", "ssid"), ("GP", "password"), ("GN", "name")]
        threading.Thread(target=self._task_run_serial_commands, args=(port, commands, True), daemon=True).start()

    def send_esp_settings(self):
        port = self.serial_ports_combobox.get()
        if not port or "Не найдены" in port: messagebox.showerror("Ошибка", "Выберите порт."); return
        commands = [ (f"SW:{self.esp_ssid_var.get()}", None), (f"SP:{self.esp_password_var.get()}", None), (f"SN:{self.esp_name_var.get()}", None), (f"SIP:{self.ip_combobox.get()}", None), (f"SPORT:{self.esp32_port_var.get()}", None), ("SAVE", None) ]
        threading.Thread(target=self._task_run_serial_commands, args=(port, commands), daemon=True).start()

    def clear_esp_eeprom(self):
        port = self.serial_ports_combobox.get()
        if not port or "Не найдены" in port: messagebox.showerror("Ошибка", "Выберите порт."); return
        if messagebox.askyesno("Подтверждение", "Вы уверены, что хотите очистить память устройства?"):
            threading.Thread(target=self._task_run_serial_commands, args=(port, [("SZEROS", None)]), daemon=True).start()

    def toggle_debug_frame(self):
        if self.debug_frame.winfo_ismapped(): self.debug_frame.grid_remove()
        else: self.debug_frame.grid()

    def send_custom_serial_command(self):
        port = self.serial_ports_combobox.get()
        command = self.custom_command_var.get()
        if not port or "Не найдены" in port: messagebox.showerror("Ошибка", "Выберите порт."); return
        if not command: messagebox.showerror("Ошибка", "Введите команду."); return
        threading.Thread(target=self._task_run_serial_commands, args=(port, [(command, None)]), daemon=True).start()
        self.custom_command_var.set("")

    def _setup_log_colors(self):
        tags = {'system':'#909090', 'wss':'blue', 'ws':'magenta', 'serial':'green', 'bridge':'#CCCC00', 'error':'red', 'raw_serial':'#606060'}
        for name, color in tags.items(): self.log_widget.tag_config(name, foreground=color, font=("Consolas", 9, "bold" if name != 'raw_serial' else 'normal'))
        self.tag_map = {"system":"[Система]     ", "wss":"[Браузер WS] ", "ws":"[Машинка WS]   ", "serial":"[Посл. Порт]     ", "bridge":"[Мост]     ", "error":"[ОШИБКА]      ", "raw_serial": "[RAW] "}
        
    def add_log_message(self, level: str, message: str):
        if level == "raw_serial" and not self.show_raw_serial_var.get(): return
        self.log_widget.configure(state='normal')
        self.log_widget.insert(tk.END, self.tag_map.get(level, ""), level)
        self.log_widget.insert(tk.END, f"{message}\n")
        self.log_widget.configure(state='disabled')
        self.log_widget.see(tk.END)

    def update_status_indicator(self, component: str, is_connected: bool | None):
        label = self.status_labels.get(component)
        if not label: return
        if is_connected is None: text, color = "Отключено", "grey"
        else: text, color = "Подключено" if is_connected else "Отключено", "green" if is_connected else "red"
        label.config(text=text, foreground=color)

    def process_gui_queue(self):
        try:
            while True:
                msg_type, *payload = self.gui_queue.get_nowait()
                if msg_type == "log": self.add_log_message(payload[0], payload[1])
                elif msg_type == "status": self.update_status_indicator(payload[0], payload[1])
                elif msg_type == "server_error": messagebox.showerror("Ошибка Cервера", "Не удалось запустить сервер."), self.disconnect_server()
                elif msg_type == "update_esp_field":
                    key, value = payload
                    if key == "ssid": self.esp_ssid_var.set(value)
                    elif key == "password": self.esp_password_var.set(value)
                    elif key == "name": self.esp_name_var.set(value)
                elif msg_type == "messagebox": messagebox.showerror("Ошибка", payload[1])
        except queue.Empty: pass
        finally: self.after(100, self.process_gui_queue)

    def on_closing(self): self.disconnect_server(); self.destroy()

# --- FIX: New, cross-platform IP discovery function ---
def get_all_local_ips():
    """
    Returns a list of all non-loopback IPv4 addresses on the machine.
    Uses 'netifaces' on Linux/macOS for reliability and 'socket' on Windows.
    """
    ip_list = []
    # Method 1: Use netifaces if available (best for Linux/macOS)
    if netifaces:
        try:
            for interface in netifaces.interfaces():
                if netifaces.AF_INET in netifaces.ifaddresses(interface):
                    for link in netifaces.ifaddresses(interface)[netifaces.AF_INET]:
                        ip = link.get('addr')
                        if ip and not ip.startswith('127.'):
                            ip_list.append(ip)
        except Exception as e:
            print(f"netifaces failed: {e}. Trying other methods.")
    
    # Method 2: Use standard socket library (best for Windows, fallback for others)
    # This check prevents running it if netifaces already succeeded.
    if not ip_list:
        try:
            hostname = socket.gethostname()
            addr_info = socket.getaddrinfo(hostname, None)
            for info in addr_info:
                if info[0] == socket.AF_INET:
                    ip = info[4][0]
                    if ip and not ip.startswith('127.'):
                        ip_list.append(ip)
        except socket.gaierror:
            # Fallback if gethostname fails (common in misconfigured environments)
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.connect(("8.8.8.8", 80))
                    ip = s.getsockname()[0]
                    if ip: ip_list.append(ip)
            except Exception as e:
                print(f"Socket fallback failed: {e}")

    # Final cleanup and adding loopback if nothing else was found
    if not ip_list:
        ip_list.append('127.0.0.1')
    
    return sorted(list(set(ip_list)))

if __name__ == "__main__":
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()