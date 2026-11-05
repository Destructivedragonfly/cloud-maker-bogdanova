#!/usr/bin/env python3

import json
import os
import subprocess
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 8080
IMG_DIR = "/var/lib/libvirt/images"
VIRSH = ["virsh", "-c", "qemu:///system"]
BASE = os.path.join(IMG_DIR, "cirros-base.img")
VM_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9-]{1,30}$")
STATE_FILE = "/var/lib/vm-api/state.json"

if os.path.exists("/dev/kvm"):
    DOMAIN_TYPE = "kvm"
else:
    DOMAIN_TYPE = "qemu"

def virsh(*args):
    result = subprocess.run(
        [*VIRSH, *args],
        capture_output=True,
        encoding="utf-8",
        errors="replace", 
    )
    
    return result.returncode == 0, (result.stdout + result.stderr).strip()

def path_to_disk(name):
    return os.path.join(IMG_DIR, f"{name}.qcow2")

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        print(f"{STATE_FILE} повреждён")
        return {}

def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp" 
    
    with open(tmp, "w") as f: # сначала записываем во временный файл, чтобы STATE_FILE не получился полузаписанным
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def vm_xml(name, memory_mb, vcpus):
    # корневой элемент
    domain = ET.Element("domain", {"type": DOMAIN_TYPE})

    # простые дочерние
    ET.SubElement(domain, "name").text = name
    ET.SubElement(domain, "memory", {"unit": "MiB"}).text = str(memory_mb)
    ET.SubElement(domain, "vcpu").text = str(vcpus)

    # <os>
    os_el = ET.SubElement(domain, "os")
    ET.SubElement(os_el, "type", {"arch": "x86_64", "machine": "q35"}).text = "hvm"
    ET.SubElement(os_el, "boot", {"dev": "hd"})

    # <features><acpi/></features>
    features = ET.SubElement(domain, "features")
    ET.SubElement(features, "acpi")

    # <devices>
    devices = ET.SubElement(domain, "devices")

    # <disk>
    disk = ET.SubElement(devices, "disk", {"type": "file", "device": "disk"})
    ET.SubElement(disk, "driver", {"name": "qemu", "type": "qcow2"})
    ET.SubElement(disk, "source", {"file": path_to_disk(name)})
    ET.SubElement(disk, "target", {"dev": "vda", "bus": "virtio"})

    # <interface>
    iface = ET.SubElement(devices, "interface", {"type": "network"})
    ET.SubElement(iface, "source", {"network": "default"})
    ET.SubElement(iface, "model", {"type": "virtio"})

    # <serial> и <console>
    serial = ET.SubElement(devices, "serial", {"type": "pty"})
    ET.SubElement(serial, "target", {"port": "0"})
    console = ET.SubElement(devices, "console", {"type": "pty"})
    ET.SubElement(console, "target", {"type": "serial", "port": "0"})

    # сериализация в строку
    return ET.tostring(domain, encoding="unicode")
    
def create_vm(name, memory_mb=256, vcpus=1):
    if not os.path.exists(BASE):
        return 500, "Выполните ./setup.sh"
        
    if os.path.exists(path_to_disk(name)) or virsh("domstate", name)[0]:
        return 409, f"'{name}' уже существует"
        
    result = subprocess.run( # создание диска
        ["qemu-img", "create", "-f", "qcow2", "-F", "qcow2",
         "-b", BASE, path_to_disk(name)],
        capture_output=True, text=True,
    )
    
    if result.returncode != 0:
        return 500, "Диск не был создан: " + result.stderr.strip()
    
    xml_file = f"/tmp/{name}.xml"
    with open(xml_file, "w") as f:
        f.write(vm_xml(name, memory_mb, vcpus))
    
    ok, message = virsh("define", xml_file) # добавляем VM в список
    os.remove(xml_file) # сразу избавляемся от временного файла
    
    if not ok:
        os.remove(path_to_disk(name))
        return 409, message
    
    state = load_state()
    state[name] = {"memory_mb": memory_mb, "vcpus": vcpus, "desired": "running"}
    save_state(state)
        
    ok, message = virsh("start", name) # запускаем домен
    return (201, message) if ok else (500, message)

def delete_vm(name):
    virsh("destroy", name)
    
    ok, message = virsh("undefine", name)
    if ok and os.path.exists(path_to_disk(name)):
        os.remove(path_to_disk(name)) # удаляем из списка
    
    state = load_state()
    state.pop(name, None)
    save_state(state)
    return ok, message

def stop_vm(name, destroy=False):
    state = load_state()
    if name in state:
        state[name]["desired"] = "stopped"
        save_state(state)
    return virsh("destroy" if destroy else "shutdown", name)

def start_vm(name):
    state = load_state()
    if name in state:
        state[name]["desired"] = "running"
        save_state(state)
    return virsh("start", name)

def reconcile():
    state = load_state()
    if not state:
        print("Сохранённых VM нет")
        return
    
    for name, vm in state.items():
        ok, status = virsh("domstate", name)
        
        if not ok:
            print(f"==> {name}: нет в libvirt, новая регистрация")
            
            if not os.path.exists(path_to_disk(name)):
                result = subprocess.run(
                    ["qemu-img", "create", "-f", "qcow2", "-F", "qcow2",
                     "-b", BASE, path_to_disk(name)],
                    capture_output=True, text=True,
                )
                if result.returncode != 0:
                    print(f"==> {name}: ошибка диска: {result.stderr.strip()}")
                    continue
                    
            xml_file = f"/tmp/{name}.xml"
            with open(xml_file, "w") as f:
                f.write(vm_xml(name, vm["memory_mb"], vm["vcpus"]))
            ok, message = virsh("define", xml_file)
            os.remove(xml_file)
            
            if not ok:
                print(f"==> {name}: ошибка define: {message}")
                continue
            status = "shut off"
        
        if vm["desired"] == "running" and status != "running":
            ok, message = virsh("start", name)
            result = "ок" if ok else "ошибка: " + message
            print(f"==> {name}: был '{status}', запуск -> {result}")

        elif vm["desired"] == "stopped" and status == "running":
            ok, message = virsh("destroy", name)
            result = "ок" if ok else "ошибка: " + message
            print(f"==> {name}: был '{status}', остановка -> {result}")
            
        else:
            print(f"==> {name}: '{status}', желаемое '{vm['desired']}' - ок")


class Handler(BaseHTTPRequestHandler):
    def send_json(self, code, data):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def reply(self, ok, output):
        # Ответ операции с кодом 200 или 500
        self.send_json(200 if ok else 500, {"output": output})

    def fail(self, code, message):
        # Ответ-ошибка
        self.send_json(code, {"error": message})

    def read_json(self):
        # Представляет тело запроса в виде dict
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
            
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self.fail(400, "тело не является валидным JSON")
            return None

    def split_path(self):
        # Разбирает HTTP запрос на параметры
        url = urlparse(self.path)
        return [p for p in url.path.split("/") if p], parse_qs(url.query)

    def check_name(self, name):
        # True если имя валидно, иначе отправляет 400 и возвращает False
        if VM_NAME_PATTERN.match(name):
            return True
            
        self.fail(400, "Имя: латиница, цифры, '-', до 30 символов")
        return False
        
    # Следующие маршруты выполняют похожие действия:
    # - сначала split_path
    # - в конце всегда отправляют JSON

    def do_GET(self):
        # JSON со списком VM
        parts, _ = self.split_path()
        
        if parts == ["vms"]:
            return self.reply(*virsh("list", "--all"))
            
        self.fail(404, "not found")

    def do_POST(self):
        # Создать/запустить/остановить VM
        parts, query = self.split_path()

        if parts == ["vms"]:
            body = self.read_json()
            if body is None:
                return
            if "name" not in body:
                return self.fail(400, 'Нужен JSON {"name": "vm1"}')
                
            name = str(body["name"])
            if not self.check_name(name):
                return
                
            try:
                mem = int(body.get("memory_mb", 256))
                vcpus = int(body.get("vcpus", 1))
            except (ValueError, TypeError):
                return self.fail(400, "memory_mb и vcpus должны быть числами")
                
            code, message = create_vm(name, mem, vcpus)
            return self.send_json(code, {"name": name, "output": message})

        if len(parts) == 3 and parts[0] == "vms":
            name, action = parts[1], parts[2]
            if not self.check_name(name):
                return
                
            if action == "start":
                return self.reply(*start_vm(name))
                
            if action == "stop":
                force = query.get("force", ["0"])[0] in ("1", "true")
                return self.reply(*stop_vm(name, force))

        self.fail(404, "not found")

    def do_DELETE(self):
        # Удалить VM
        parts, _ = self.split_path()
        if len(parts) == 2 and parts[0] == "vms":
        
            name = parts[1]
            if not self.check_name(name):
                return
                
            ok, message = delete_vm(name)
            return self.send_json(200 if ok else 500, {"name": name, "output": message})
        self.fail(404, "not found")
        
if __name__ == "__main__":
    reconcile()
    print("API в работе")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()

