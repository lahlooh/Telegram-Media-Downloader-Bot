const fs = require("fs");
const path = require("path");

function getPythonInterpreter() {
  if (process.env.PYTHON_PATH) {
    return process.env.PYTHON_PATH;
  }

  const venvs = [
    path.join(__dirname, ".venv", "Scripts", "python.exe"),
    path.join(__dirname, "venv", "Scripts", "python.exe"),
    path.join(__dirname, ".venv", "bin", "python"),
    path.join(__dirname, "venv", "bin", "python"),
  ];

  for (const p of venvs) {
    if (fs.existsSync(p)) {
      return p;
    }
  }

  return process.platform === "win32" ? "python" : "python3";
}

module.exports = {
  apps: [
    {
      name: "telegram-downloader-bot",
      script: "bot.py",
      interpreter: getPythonInterpreter(),
      interpreter_args: "-u",
      autorestart: true,
      restart_delay: 2000,
      max_memory_restart: "1G",
      watch: false,
      error_file: "./logs/pm2-error.log",
      out_file: "./logs/pm2-out.log",
      merge_logs: true,
      time: true,
      env: {
        PYTHONUNBUFFERED: "1",
        PYTHONIOENCODING: "utf-8",
        NODE_ENV: "production",
      },
    },
  ],
};
