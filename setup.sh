#!/usr/bin/env bash
set -euo pipefail # правильная обработка ошибок

if [ "$EUID" -ne 0 ]; then # проверяем, что права root, иначе остальные команды упадут
	echo "Запустите: sudo ./setup.sh"
	exit 1
fi

echo "--> 1/4 Установка пакетов libvirt, virsh, QEMU, qemu-img, python3, jq" # jq для изменения JSON прямо из bash (test.sh)
apt-get update 
apt-get install -y qemu-system-x86 libvirt-daemon-system libvirt-clients qemu-utils python3 curl jq # автоматически отвечает Y

echo "--> 2/4 Запуск демона libvirtd"
systemctl enable --now libvirtd 2>/dev/null || systemctl enable --now libvirt-daemon.socket # 2>/dev/null съедает текст ошибки, если первый вариант не запустился

echo "--> 3/4 Включение сети default"
virsh -c qemu:///system net-start default 2>/dev/null || true # true нужен, чтобы команда не упала при повторном запуске, когда сеть уже включена
virsh -c qemu:///system net-autostart default # включить автозапуск сети

echo "--> 4/4 Установка базового образа диска CirrOS"
IMG=/var/lib/libvirt/images/cirros-base.img

if [ ! -f "$IMG" ]; then # если нет, то скачиваем
	curl -L -o "$IMG" https://download.cirros-cloud.net/0.6.2/cirros-0.6.2-x86_64-disk.img
	chmod 644 "$IMG"
fi

echo
echo

if [ -e /dev/kvm ]; then
	echo "OK"
else 
	echo "KVM не найден, далее будет использоваться программная эмуляция"
fi

echo "Запустите API: sudo python3 app.py"
