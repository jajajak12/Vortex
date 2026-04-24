module.exports = {
  apps: [{
    name: 'vortex',
    script: 'scanner.py',
    interpreter: '/home/prospera/vortex/venv/bin/python3',
    args: '-u',
    cwd: '/home/prospera/vortex',
    restart_delay: 5000,
    max_restarts: 10,
    exp_backoff_restart_delay: 100,
    env: {
      PYTHONUNBUFFERED: '1',
      VORTEX_EXCHANGE: 'binance',
      VORTEX_BINANCE_KEY: 'CiKNDu2fcKk5YYVTZHWoXxSTERlrlEeIMCnZF7XWzIyRG45HMfPxTjdetaF4MgGV',
      VORTEX_BINANCE_SECRET: 'jy9WmV1F4OOJ5FvBVySD7HfzJKPb9uEQdRYOqdq1r1YtaPnn1Ntkr9gnQMVMoT71',
      VORTEX_TELEGRAM_TOKEN: '8718519083:AAGVu_qD1cYhDXWK_TIjoWLIwe-C0G0Hea0',
      VORTEX_TELEGRAM_CHAT_ID: '5396263034',
    },
    error_file: '/home/prospera/vortex/logs/pm2-error.log',
    out_file: '/home/prospera/vortex/logs/pm2-out.log',
    time: true,
  }]
};
