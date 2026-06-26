from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers import health, setup, runs, books
from app.config import settings

app = FastAPI(
    title="Encuentro Noticias Backend",
    description="Microservicio de FastAPI para automatizar el raspado de reseñas de libros y validación por IA.",
    version="1.0.0"
)

# CORS middleware configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(health.router)
app.include_router(setup.router)
app.include_router(runs.router)
app.include_router(books.router)
