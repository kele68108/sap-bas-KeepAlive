FROM mcr.microsoft.com/playwright/python:v1.42.0-jammy

WORKDIR /app
COPY requirements.txt .

# 安装 Python 依赖
RUN pip install --no-cache-dir -r requirements.txt

# 将当前目录的所有代码复制到容器中
COPY . .

# 启动核心逻辑 (CF 会动态注入 $PORT 给 Flask)
CMD ["python", "-u", "app.py"]
