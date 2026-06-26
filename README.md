# Encuentro Noticias - Backend API

Microservicio backend desarrollado en Python con **FastAPI** para la recolección, filtrado y validación por Inteligencia Artificial (OpenAI) de reseñas, críticas, noticias o artículos referentes a un catálogo de libros. Toda la entrada de datos, logs, configuración y salida de resultados se gestiona mediante una hoja de cálculo centralizada de **Google Sheets**.

---

## 🎯 Objetivo General

Automatizar la búsqueda y curación de artículos relacionados con libros específicos. El sistema:
1. Lee los libros marcados como `pendiente` (o vacíos) en la pestaña `Libros`.
2. Genera combinaciones de búsquedas nacionales e internacionales.
3. Raspa candidatos del buscador DuckDuckGo (HTML).
4. Normaliza las URLs y comprueba duplicados (SHA-256 de ISBN + URL).
5. Extrae el contenido del artículo (usando `trafilatura` con fallback a `BeautifulSoup`).
6. Valida con la API de OpenAI (usando Pydantic Structured Outputs en `gpt-4o-mini`) que el texto habla verdaderamente del libro, escribe un resumen periodístico de unas 120 palabras en español y categoriza la publicación.
7. Almacena las reseñas aprobadas, los descartes (con su respectivo motivo) y los logs técnicos en sus correspondientes pestañas de Google Sheets.

---

## 📋 Estructura de Pestañas en Google Sheets

La hoja de cálculo (`GOOGLE_SHEET_ID`) debe compartirse con el correo electrónico de la Service Account de Google. Al ejecutar el endpoint de configuración, se asegurará la existencia de las siguientes pestañas:

*   **`Libros`**: Contiene la base de datos de libros a evaluar.
    *   *Columnas*: `ISBN`, `Título del libro`, `Autor del libro`, `Estado`, `Última ejecución`, `Reseñas encontradas`, `Observaciones`.
    *   *Estados posibles*: `pendiente`, `procesando`, `completado`, `sin_resultados`, `error`.
*   **`Reseñas`**: Guarda las publicaciones aceptadas que superan el score mínimo de validación.
    *   *Columnas*: `ISBN`, `Título del libro`, `Autor del libro`, `Query`, `URL`, `URL normalizada`, `Título del artículo`, `Título del libro detectado por IA`, `Autor del libro detectado por IA`, `Medio de publicación`, `Autor de la publicación`, `Fecha de publicación`, `Idioma original`, `Categoría`, `Resumen`, `Score de coincidencia`, `Tipo de contenido`, `Fecha de extracción`, `Hash deduplicación`, `Estado`.
*   **`Descartes`**: URLs evaluadas pero descartadas por algún motivo.
    *   *Columnas*: `ISBN`, `Título del libro`, `Autor del libro`, `Query`, `URL`, `Título detectado`, `Motivo de descarte`, `Score de coincidencia`, `Fecha de extracción`.
    *   *Motivos*: `duplicado`, `no menciona el libro`, `habla solo del autor`, `extracción fallida`, `texto insuficiente`, `score bajo`, `error HTTP`, `error OpenAI`, `error desconocido`.
*   **`Logs`**: Registro detallado de la ejecución en segundo plano.
    *   *Columnas*: `Run ID`, `Fecha`, `Nivel`, `ISBN`, `Acción`, `Mensaje`, `Detalle`.
*   **`Config`**: Configuraciones leídas en cada ejecución que prevalecen sobre el archivo `.env`.
    *   *Columnas*: `Clave`, `Valor`, `Descripción`.
    *   *Claves inicializadas*: `MAX_BOOKS_PER_RUN`, `MAX_SEARCH_PAGES_PER_QUERY`, `MAX_CANDIDATES_PER_BOOK`, `MIN_MATCH_SCORE`, `OPENAI_MODEL`.

---

## ⚙️ Configuración y Variables de Entorno

Crea un archivo `.env` en la raíz del proyecto basándote en `.env.example`:

```bash
APP_ENV=local
ADMIN_TOKEN=tu_token_secreto_aqui # Dejar vacío para desactivar seguridad en local
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
GOOGLE_SERVICE_ACCOUNT_JSON_BASE64=eyJ...
GOOGLE_SHEET_ID=121MqN4CFpCBOvJ__cOlpcIvKx3gdNu5tuKtN624td8c
GOOGLE_SHARE_WITH_EMAIL=
MIN_MATCH_SCORE=75
MAX_BOOKS_PER_RUN=10
MAX_SEARCH_PAGES_PER_QUERY=3
MAX_CANDIDATES_PER_BOOK=50
REQUEST_TIMEOUT_SECONDS=20
SCRAPER_USER_AGENT=Mozilla/5.0 compatible encuentro-noticias
```

### 🔑 Generar Service Account JSON en Base64

Para configurar la variable `GOOGLE_SERVICE_ACCOUNT_JSON_BASE64`, debes codificar el archivo JSON de tu service account de Google Cloud:

*   **En Windows (PowerShell)**:
    ```powershell
    [Convert]::ToBase64String([System.IO.File]::ReadAllBytes("ruta\a\tu-archivo-credenciales.json"))
    ```
*   **En Linux/Mac (Bash)**:
    ```bash
    cat ruta/a/tu-archivo-credenciales.json | base64 -w 0
    ```

Copia el string resultante y pégalo en el valor de la variable en tu archivo `.env`.

---

## 🚀 Ejecución en Local

1.  **Crear entorno virtual e instalar dependencias**:
    ```bash
    python -m venv venv
    # En Windows:
    .\venv\Scripts\activate
    # En Linux/Mac:
    source venv/bin/activate

    pip install -r requirements.txt
    ```

2.  **Ejecutar el servidor local**:
    ```bash
    uvicorn app.main:app --reload
    ```
    La API estará disponible en `http://localhost:8000`. La documentación interactiva (Swagger UI) se puede acceder en `http://localhost:8000/docs`.

3.  **Ejecutar pruebas unitarias**:
    ```bash
    pytest
    ```

> [!NOTE]
> **Nota sobre codificación en Windows (PowerShell)**:
> Si en la terminal de PowerShell observas caracteres con problemas de codificación (como `EjecuciÃ³n`, `reseÃ±as` o `IntroducciÃ³n`), esto es únicamente un comportamiento visual de la terminal de Windows. Google Sheets recibe y escribe los caracteres correctamente en formato UTF-8 nativo.
> Para corregir la visualización en tu sesión de PowerShell, ejecuta el siguiente comando antes de lanzar el servidor o consultar resultados:
> ```powershell
> chcp 65001
> ```


---

## 🐳 Ejecución con Docker / Dokploy

Este microservicio viene listo para ser empaquetado y desplegado utilizando Docker en Dokploy o cualquier VPS.

1.  **Construir la imagen de Docker**:
    ```bash
    docker build -t encuentro-noticias-backend .
    ```

2.  **Ejecutar el contenedor**:
    ```bash
    docker run -d -p 8000:8000 --env-file .env encuentro-noticias-backend
    ```

Dokploy mapeará automáticamente el puerto interno `8000` con el puerto público configurado en la plataforma.

---

## 🛠️ Endpoints de la API

Si `ADMIN_TOKEN` está configurado en el archivo de entorno, todos los endpoints (excepto `/health`) requerirán la cabecera `X-Admin-Token` con el valor exacto de la clave configurada.

### 1. Salud del Sistema
*   **`GET /health`**
    *   *Descripción*: Verifica que el servicio esté levantado.
    *   *Respuesta*: `{"status": "ok"}`

### 2. Configuración de Google Sheets
*   **`POST /setup/ensure-sheet`**
    *   *Descripción*: Comprueba la conexión con Google Sheets, inicializa las pestañas ausentes y escribe las cabeceras/configuraciones iniciales sin borrar datos.
    *   *Respuesta*:
        ```json
        {
          "success": true,
          "message": "Google Sheet verified and prepared successfully.",
          "sheet_id": "121MqN4CFpCBOvJ__cOlpcIvKx3gdNu5tuKtN624td8c",
          "sheet_url": "https://docs.google.com/spreadsheets/d/121..."
        }
        ```

### 3. Ejecución de Tareas en Segundo Plano
*   **`POST /runs`**
    *   *Descripción*: Inicia la búsqueda para los libros pendientes.
    *   *Cuerpo (JSON)*:
        ```json
        {
          "limit_books": 10,
          "dry_run": false
        }
        ```
    *   *Respuesta*: `{"run_id": "run_a8b9c1d2", "message": "Background scraping run started..."}`

*   **`POST /runs/book/{isbn}`**
    *   *Descripción*: Inicia la búsqueda y validación solo para un libro específico por su ISBN.
    *   *Cuerpo (JSON)*:
        ```json
        {
          "dry_run": false
        }
        ```

*   **`GET /runs/{run_id}`**
    *   *Descripción*: Obtiene el estado actual y los registros (logs) en tiempo real para una ejecución específica.
    *   *Respuesta*:
        ```json
        {
          "run_id": "run_a8b9c1d2",
          "status": "completed",
          "books_total": 1,
          "books_processed": 1,
          "books_completed": 1,
          "books_failed": 0,
          "books_no_results": 0,
          "message": "Ejecución completada...",
          "logs": [...]
        }
        ```

### 4. Consultas y Deduplicación
*   **`GET /books/status`**
    *   *Descripción*: Devuelve un contador del estado de todos los libros (pendiente, procesando, completado, sin_resultados, error).

*   **`POST /dedupe/rebuild`**
    *   *Descripción*: Recalcula todos los hashes de deduplicación en la pestaña `Reseñas` y los actualiza en lote si están vacíos o incorrectos.
