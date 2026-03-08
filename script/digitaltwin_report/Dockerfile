FROM plantwin:1.0

WORKDIR /app
COPY . /app

RUN pip3 install --no-cache-dir "typing_extensions>=4.12" "dash==2.17.1" plotly

EXPOSE 8003 8060
CMD ["python3", "-u", "report_server.py"]
