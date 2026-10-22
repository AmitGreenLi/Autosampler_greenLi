# Glovebox Pi USB serial bridge

The operator Pi runs the autosampler GUI. The glovebox Pi owns the USB cables
to GRBL, the A&D scale adapter, and any other USB serial devices. The existing
USB Ethernet gadget link carries an authenticated SSH session between the two
Pis. No new listening service or open control port is required.

```text
operator Pi (dcm, USB host, 10.12.194.2)
  autosampler GUI ── SSH over USB ── glovebox Pi (dcm, USB gadget, 10.12.194.1)
                                  ├── USB serial → GRBL
                                  └── USB serial → scale
```

Confirmed addresses for this installation:

| Pi | Connection | Address |
|---|---|---|
| `operator` | LAN, used by Windows for uploads | `192.168.2.91` |
| `operator` | Pi-to-Pi USB network | `10.12.194.2` |
| `glovebox` | Pi-to-Pi USB network | `10.12.194.1` |

Both Pi accounts are named `dcm`.

Local USB mode remains the default. In hardware mode, the GUI probes the
glovebox Pi in the background and offers to switch if its SSH bridge responds.
It never connects a device or moves the machine automatically. The GRBL and
scale menus list every serial device returned by the glovebox Pi. A device
must be assigned to each role; additional serial sensors can be discovered,
but using their measurements requires a sensor-specific controller. GPIO and
I²C devices are not covered by this serial bridge.

## 1. Confirm the USB network

On the operator Pi:

```bash
ip route get 10.12.194.1
ping -c 2 10.12.194.1
```

The route should use the USB network interface and source address
`10.12.194.2`.

## 2. Put the current source on both Pis

First, from Windows PowerShell in the project directory, upload the updated
source and guide to the operator Pi. Run these as separate commands:

```powershell
Set-Location -LiteralPath 'C:\Users\Amit\Documents\ChatGPT\New project'
scp -r .\autosampler .\tests .\docs dcm@192.168.2.91:/home/dcm/autosampler/
```

Then, from the operator Pi, copy the serial bridge source to the inner Pi:

```bash
ssh dcm@10.12.194.1 'mkdir -p ~/autosampler'
scp -r ~/autosampler/autosampler dcm@10.12.194.1:~/autosampler/
```

The inner Pi needs the Python package source, including `remote_bridge.py`.
Its virtual environment must be created on that Pi, not copied from Windows
or the outer Pi.

On the glovebox Pi (locally or through SSH):

```bash
sudo apt update
sudo apt install -y python3-venv
cd ~/autosampler
python3 -m venv .venv
.venv/bin/python -m pip install 'pyserial>=3.5'
id -nG
```

The SSH user on the glovebox Pi needs membership in the group owning its
serial devices, commonly `dialout`. If needed, add it with
`sudo usermod -aG dialout "$USER"`, then log out and back in. The inner Pi
does not need PySide6 or a desktop to run the bridge.

## 3. Allow passwordless SSH from operator to glovebox

The GUI uses noninteractive SSH, so a key and a previously verified host key
are required. On the operator Pi, first connect manually and verify the
host key:

```bash
ssh dcm@10.12.194.1
```

Exit that session. If the operator Pi has no SSH key yet, create one with
`ssh-keygen -t ed25519`. Then authorize its public key on the glovebox Pi:

```bash
ssh-copy-id dcm@10.12.194.1
ssh -o BatchMode=yes dcm@10.12.194.1 hostname
```

The last command must print `glovebox` without prompting for a password.

## 4. Verify bridge discovery without opening hardware

On the operator Pi:

```bash
printf '{"op":"list"}\n' | ssh -T -o BatchMode=yes dcm@10.12.194.1 \
  'cd ~/autosampler && .venv/bin/python -m autosampler.remote_bridge'
```

The response is one JSON line with `"ok":true` and a `devices` array. Each
attached USB serial device has its own `/dev/ttyUSB*` or `/dev/ttyACM*` path
and metadata. This request opens no serial port and sends no GRBL command.

## 5. Select the source in the GUI

From the operator Pi's desktop session:

```bash
cd ~/autosampler
.venv/bin/python -m autosampler.main --hardware --fullscreen
```

If the glovebox prompt appears, accept it. Otherwise choose **Glovebox Pi over
USB** beside **Device source**, enter `dcm@10.12.194.1` in
**Glovebox SSH**, and tap **Refresh devices**. Assign the GRBL and scale ports
by checking their USB descriptions and unplugging one at a time if necessary.
Then tap **Connect GRBL** and **Connect Scale** separately. Connection to GRBL
still invalidates coordinate trust and requires Home before motion.

The two source modes save separate device assignments. Serial-less USB
adapters are remembered by USB location; keep them in the same physical USB
sockets. If two adapters cannot be distinguished, the app asks for a manual
selection rather than guessing.

## Troubleshooting

- If `ping` fails, restore the Pi-to-Pi USB gadget network before starting the
  app. The Windows LAN address of the operator Pi is not the gadget address.
- If SSH asks for a password in step 3, set up the key before using the GUI.
- If discovery reports no devices, run
  `~/autosampler/.venv/bin/python -m serial.tools.list_ports -v` on the inner Pi.
- If a port is listed but opening fails, check the inner Pi user's `dialout`
  membership and close any serial terminal already using that port.
- If the USB link drops during a GRBL operation, the app treats the serial
  connection as lost and requires reconnection and Home. A physical emergency
  stop must remain available outside the glovebox.
