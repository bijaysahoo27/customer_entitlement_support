.PHONY: up down


up:
	docker compose up --build --wait --wait-timeout 60
	@printf '%s\n' 'API docs http://127.0.0.1:8000/docs' 'Health check http://127.0.0.1:8000/health'

down:
	docker compose down --remove-orphans
