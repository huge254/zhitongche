FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple flask pymysql redis elasticsearch==8.13.1

COPY app.py .
EXPOSE 8000
CMD ["python", "app.py"]
