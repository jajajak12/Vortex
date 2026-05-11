const fs = require('fs');
const path = require('path');

function loadDotEnv(filePath) {
  const env = {};
  const raw = fs.readFileSync(filePath, 'utf8');
  for (const line of raw.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const eq = trimmed.indexOf('=');
    if (eq === -1) continue;
    const key = trimmed.slice(0, eq).trim();
    const value = trimmed.slice(eq + 1).trim();
    env[key] = value;
  }
  return env;
}

const envPath = path.join(__dirname, '.env');
const fileEnv = loadDotEnv(envPath);

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
      ...fileEnv,
    },
    error_file: '/home/prospera/vortex/logs/pm2-error.log',
    out_file: '/home/prospera/vortex/logs/pm2-out.log',
    time: true,
  }]
};
