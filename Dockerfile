FROM mcr.microsoft.com/playwright/python:v1.42.0-jammy

WORKDIR /app
COPY requirements.txt .

# 安装 Python 依赖
RUN pip install --no-cache-dir -r requirements.txt

# ==========================================
# 🛡️ 时区护城河：静默安装 tzdata 并将容器主板时间焊死在上海时区
# ==========================================
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Asia/Shanghai
RUN apt-get update && apt-get install -y tzdata && \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone && \
    rm -rf /var/lib/apt/lists/*

# 将当前目录的所有代码复制到容器中
COPY . .

# 启动核心逻辑 (CF 会动态注入 $PORT 给 Flask)
CMD ["python", "-u", "app.py"]
