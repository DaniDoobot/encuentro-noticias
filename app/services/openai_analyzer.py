from openai import OpenAI
from pydantic import BaseModel, Field
from app.config import settings
from typing import Dict, Any, Optional
import json
import logging

logger = logging.getLogger("encuentro-noticias")

class BookValidationResult(BaseModel):
    is_valid: bool = Field(description="true si el artículo habla realmente del libro concreto y no solamente del autor; false en caso contrario.")
    match_score: int = Field(description="Puntuación de coincidencia y relevancia de la reseña con el libro, de 0 a 100.")
    reason: str = Field(description="Motivo detallado de la validación o descarte.")
    detected_book_title: str = Field(description="Título del libro tal como se detecta en el texto.")
    detected_book_author: str = Field(description="Autor del libro tal como se detecta en el texto.")
    content_type: str = Field(description="Tipo de contenido del artículo (ej. reseña, noticia, entrevista, crítica, etc.).")
    publication_name: str = Field(description="Nombre del medio de publicación detectado.")
    publication_author: str = Field(description="Nombre del autor de la publicación/artículo detectado.")
    publication_date: str = Field(description="Fecha de publicación detectada (en formato YYYY-MM-DD si es posible).")
    language: str = Field(description="Idioma original del artículo.")
    category: str = Field(description="Categoría a la que pertenece el artículo, debe ser exactamente una de: Cultura/Educación, Política, Economía, Historia, Religión, Sociedad, Ciencia, Tecnología, Literatura, Otros.")
    summary: str = Field(description="Explicación en español de aproximadamente 120 palabras de dónde, cómo y qué se menciona en el artículo sobre el libro buscado, detallando su relevancia.")

class OpenAIAnalyzer:
    def __init__(self):
        # We initialize client lazily to avoid crashing on start if API key is missing during build
        self._client = None

    def get_client(self) -> OpenAI:
        if self._client is None:
            if not settings.OPENAI_API_KEY:
                raise ValueError("OPENAI_API_KEY is not configured in environment variables.")
            self._client = OpenAI(api_key=settings.OPENAI_API_KEY)
        return self._client

    def analyze_article(
        self,
        isbn: str,
        book_title: str,
        book_author: str,
        query: str,
        url: str,
        article_title: str,
        article_text: str,
        detected_date: str = "",
        detected_author: str = "",
        detected_medium: str = "",
        model_override: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Queries OpenAI to validate if the article relates to the specific book.
        Returns a dictionary matching the BookValidationResult schema.
        """
        client = self.get_client()
        model = model_override or settings.OPENAI_MODEL
        
        # If the model is 'gpt-4.1-mini', OpenAI SDK will error because it doesn't exist.
        # We automatically map it to 'gpt-4o-mini' if we detect 'gpt-4.1-mini' or similar.
        if "gpt-4.1-mini" in model:
            model = "gpt-4o-mini"

        logger.info(f"Analyzing article via OpenAI model: {model}")

        system_prompt = (
            "Eres un analista literario y periodista experto de habla hispana.\n"
            "Tu tarea es evaluar si un artículo web habla sobre un libro específico y extraer metadatos relevantes.\n\n"
            "REGLAS CRÍTICAS DE VALIDACIÓN Y CRITERIOS FLEXIBLES:\n"
            "1. 'is_valid' solo debe ser true si el artículo trata sobre el libro concreto indicado. Esto incluye reseñas completas, críticas, noticias de lanzamiento, entrevistas al autor sobre este libro, o artículos donde el libro sea un tema principal, parte relevante o se mencione de forma sustancial en el cuerpo.\n"
            "2. NO exijas coincidencia literal del título del libro. Acepta variantes parciales, naturales o reordenadas si el artículo habla inequívocamente de la misma obra (ej. 'la nueva Biblia de YOUCAT' es una variante válida de 'YOUCAT Biblia'). El título del libro puede aparecer solo en el cuerpo del artículo y no en el título de la página.\n"
            "3. Si el artículo solo menciona al autor pero NO al libro concreto, 'is_valid' DEBE ser false.\n"
            "4. CUIDADO CON TÍTULOS GENÉRICOS: Para títulos muy genéricos o cortos (ej. 'Símbolos', 'Biblia', 'Diario', 'Introducción a los símbolos'), NO debes aceptar coincidencias parciales pobres. En estos casos, exige señales adicionales claras (coincidencia de autor, editorial, ISBN, subtítulo o contexto temático muy específico con múltiples menciones en el cuerpo) para validar la coincidencia.\n"
            "5. Si la mención del libro es puramente tangencial/secundaria (ej. solo aparece en una lista bibliográfica sin comentario o se nombra de pasada en una oración sin aportar nada sobre la obra), 'is_valid' DEBE ser false.\n"
            "6. Si la coincidencia del título es casual y trata realmente de otra obra o de un tema homónimo distinto, 'is_valid' DEBE ser false.\n"
            "7. Los datos de 'ISBN' o 'Autor del libro' del libro buscado pueden estar en blanco. En ese caso, evalúa la coincidencia basándote en los campos provistos sin penalizar por la ausencia de datos opcionales.\n"
            "8. La categoría ('category') DEBE ser estrictamente una de las siguientes opciones:\n"
            "   - Cultura/Educación, Política, Economía, Historia, Religión, Sociedad, Ciencia, Tecnología, Literatura, Otros.\n"
            "9. El resumen ('summary') DEBE estar escrito en ESPAÑOL y tener alrededor de 120 palabras. NO debe ser un resumen genérico del artículo completo, sino una explicación útil que indique DÓNDE, CÓMO y QUÉ menciona el artículo respecto al libro en cuestión, detallando si está centrado en el libro o lo menciona parcialmente y qué dice de él.\n\n"
            "GUÍA DE PUNTUACIÓN ('match_score'):\n"
            "- 0: No hay relación suficiente con el libro o habla de otra obra.\n"
            "- 30-49: Mención débil o tangencial (no suficiente para ser validado).\n"
            "- 50-69: Mención relevante pero no es una reseña completa (ej. se comenta en un párrafo extenso dentro de un artículo más amplio).\n"
            "- 70-89: Artículo/crítica claramente sobre el libro con análisis sustancial.\n"
            "- 90-100: Reseña muy clara y dedicada, donde el título o la gran parte del artículo se centran directamente en la obra.\n"
        )

        user_content = (
            f"DATOS DEL LIBRO BUSCADO:\n"
            f"- ISBN: {isbn}\n"
            f"- Título del libro: {book_title}\n"
            f"- Autor del libro: {book_author}\n\n"
            f"DATOS DE LA BÚSQUEDA Y EXTRACCIÓN:\n"
            f"- Query usada: {query}\n"
            f"- URL del artículo: {url}\n"
            f"- Título del artículo: {article_title}\n"
            f"- Medio detectado: {detected_medium}\n"
            f"- Autor detectado: {detected_author}\n"
            f"- Fecha detectada: {detected_date}\n\n"
            f"CONTENIDO DEL ARTÍCULO:\n"
            f"\"\"\"\n{article_text[:6000]}\n\"\"\"" # Cap at 6000 chars to avoid token inflation
        )

        try:
            # Attempt to use Structured Outputs
            completion = client.beta.chat.completions.parse(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}
                ],
                response_format=BookValidationResult,
                timeout=45.0
            )
            
            result_obj = completion.choices[0].message.parsed
            if result_obj:
                return result_obj.model_dump()
            else:
                raise ValueError("Parsing returned empty result")

        except Exception as e:
            logger.warning(f"Structured outputs failed, falling back to JSON mode: {e}")
            # Fallback to standard chat completion with JSON mode
            try:
                fallback_prompt = system_prompt + "\nDevuelve un objeto JSON que coincida exactamente con este formato:\n" + json.dumps({
                    "is_valid": True,
                    "match_score": 85,
                    "reason": "Explicación breve",
                    "detected_book_title": "Título",
                    "detected_book_author": "Autor",
                    "content_type": "reseña",
                    "publication_name": "NombreMedio",
                    "publication_author": "AutorArticulo",
                    "publication_date": "2023-01-01",
                    "language": "es",
                    "category": "Literatura",
                    "summary": "Resumen periodístico en español de unas 120 palabras..."
                }, indent=2)

                response = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": fallback_prompt},
                        {"role": "user", "content": user_content}
                    ],
                    response_format={"type": "json_object"},
                    timeout=45.0
                )
                
                content = response.choices[0].message.content
                data = json.loads(content)
                
                # Manual validation and schema mapping to ensure all keys exist
                return {
                    "is_valid": bool(data.get("is_valid", False)),
                    "match_score": int(data.get("match_score", 0)),
                    "reason": str(data.get("reason", "")),
                    "detected_book_title": str(data.get("detected_book_title", "")),
                    "detected_book_author": str(data.get("detected_book_author", "")),
                    "content_type": str(data.get("content_type", "")),
                    "publication_name": str(data.get("publication_name", detected_medium or "")),
                    "publication_author": str(data.get("publication_author", detected_author or "")),
                    "publication_date": str(data.get("publication_date", detected_date or "")),
                    "language": str(data.get("language", "")),
                    "category": str(data.get("category", "Otros")),
                    "summary": str(data.get("summary", ""))
                }
            except Exception as e_fallback:
                logger.error(f"OpenAI fallback query also failed: {e_fallback}")
                raise RuntimeError(f"error OpenAI: {str(e_fallback)}")

openai_analyzer = OpenAIAnalyzer()
