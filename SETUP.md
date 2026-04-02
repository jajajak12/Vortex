# TRADING AGENT — Setup Guide
# Strategy 1: Fresh Liquidity Grab + Rejection

## STEP 1: Upload ke VPS
```bash
# Di local kamu, upload folder ini ke VPS
scp -r trading-agent/ user@YOUR_VPS_IP:~/
```

## STEP 2: Install dependencies
```bash
cd ~/trading-agent
pip install -r requirements.txt
```

## STEP 3: Isi config.py
Edit file config.py dan isi:
- TELEGRAM_BOT_TOKEN  → dari @BotFather di Telegram
- TELEGRAM_CHAT_ID    → ID chat kamu (bisa cek via @userinfobot)
- BINANCE_API_KEY     → dari Binance account settings
- BINANCE_API_SECRET  → dari Binance account settings

## STEP 4: Test jalankan
```bash
python3 scanner.py
```

## STEP 5: Jalankan background (24/7 di VPS)
```bash
# Pakai screen agar tetap jalan setelah terminal ditutup
screen -S trading-agent
python3 scanner.py

# Detach screen: Ctrl+A lalu D
# Reattach screen: screen -r trading-agent
```

## STEP 6: Auto-restart kalau crash (opsional)
```bash
# Buat file service
sudo nano /etc/systemd/system/trading-agent.service
```

Isi dengan:
```
[Unit]
Description=Trading Agent Strategy 1
After=network.target

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/home/YOUR_USERNAME/trading-agent
ExecStart=/usr/bin/python3 scanner.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable trading-agent
sudo systemctl start trading-agent
sudo systemctl status trading-agent
```

## CARA BUAT TELEGRAM BOT:
1. Buka Telegram, cari @BotFather
2. Ketik /newbot
3. Ikuti instruksi, simpan TOKEN yang diberikan
4. Cari @userinfobot untuk dapat CHAT_ID kamu
5. Isi keduanya di config.py

## CARA DAPAT BINANCE API KEY:
1. Login Binance → Account → API Management
2. Create API Key
3. Enable "Read Info" saja (agent ini hanya baca data, tidak trading otomatis)
4. Simpan API Key & Secret ke config.py
