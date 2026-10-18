release: python -m stbot.setup
web: gunicorn 'stbot.web:create_app()' --workers 1 --threads 4 --bind 0.0.0.0:$PORT --timeout 60
