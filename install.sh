#!/usr/bin/env bash
set -e

echo "=== PiMatrixOS Automated Installer ==="
echo "Target OS: Raspberry Pi OS Lite (32-bit)"
echo

# Require sudo
if [ "$EUID" -ne 0 ]; then
  echo "Please run this installer with sudo:"
  echo "  sudo ./install.sh"
  exit 1
fi

echo "==> Updating system..."
apt update
apt upgrade -y

echo "==> Installing required packages..."
apt install -y \
  git \
  build-essential \
  python3 \
  python3-pil \
  python3-dev \
  swig \
  cython3 \
  python3-setuptools \
  python3-wheel

cd /home/pi || exit 1

echo "==> Installing rpi-rgb-led-matrix..."
if [ ! -d rpi-rgb-led-matrix ]; then
  git clone https://github.com/hzeller/rpi-rgb-led-matrix.git
fi

cd rpi-rgb-led-matrix
make

cd bindings/python
make build-python
python3 setup.py install

cd /home/pi || exit 1

echo "==> Installing PiMatrixOS..."
if [ ! -d pimatrixos ]; then
  git clone https://github.com/dominikelektricar/pimatrixos.git
fi

cd pimatrixos
chmod +x launcher.py

echo
echo "✅ PiMatrixOS installation complete."
echo
echo "To start PiMatrixOS:"
echo "  cd ~/pimatrixos"
echo "  sudo python3 launcher.py"
