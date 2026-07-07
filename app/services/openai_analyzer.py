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
    summary: str = Field(description="Selección de entre 1 y 3 frases del cuerpo del artículo copiadas estrictamente de forma literal que hablen de la obra, separadas por espacios. Sin parafrasear, resumir ni reescribir. Si no hay frases válidas, dejar vacío.")
    score_justification: str = Field(description="Explicación detallada interna de por qué se ha asignado esta puntuación de relevancia o coincidencia. Este campo no es el resumen y no se publica en WordPress.")

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
        model_override: Optional[str] = None,
        metadata_detected: Optional[Dict[str, Any]] = None
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
            "REGLAS CRÍTICAS DE VALIDACIÓN Y CRITERIOS ULTRA-FLEXIBLES:\n"
            "1. 'is_valid' debe ser true cuando hay una mención real del libro en el artículo, por mínima o breve que sea, incluyendo comentarios de pasada, adaptaciones, representaciones, debates o listados comentados. Solo debe ser false si el artículo NO menciona la obra en absoluto, habla de otro libro distinto, habla únicamente de la figura del autor sin nombrar esta obra, o si el libro aparece exclusivamente en un listado automático, índice, catálogo, bibliografía de pie de página o recomendados sin comentarios ni contexto.\n"
            "2. COHERENCIA OBLIGATORIA: Si consideras que el artículo menciona el libro de forma real y el score es >= 1, 'is_valid' DEBE ser true. Si el score es 0, 'is_valid' DEBE ser false. No mezcles 'is_valid: false' con un score mayor a cero.\n"
            "3. NO exijas coincidencia literal del título del libro. Acepta variantes parciales, naturales o reordenadas si el artículo habla inequívocamente de la misma obra.\n"
            "4. CUIDADO CON TÍTULOS GENÉRICOS: Para títulos muy genéricos o cortos (ej. 'Símbolos', 'Biblia', 'Diario'), exige alguna señal contextual clara de que se referieren al libro buscado para evitar falsos positivos con otros temas homónimos.\n"
            "5. La categoría ('category') DEBE ser estrictamente una de las siguientes opciones:\n"
            "   - Cultura/Educación, Política, Economía, Historia, Religión, Sociedad, Ciencia, Tecnología, Literatura, Otros.\n"
            "6. El campo 'summary' DEBE consistir únicamente en la selección de entre 1 y 3 frases textuales y literales del cuerpo del artículo que hablen directamente del libro evaluado. Las frases deben ser copiadas palabra por palabra sin resumir, parafrasear, corregir ni reescribir. Prioriza frases que mencionen el título del libro o al autor (si la relación es inequívoca) y aporten información sobre la obra (contenido, temática, valoración, recepción). No utilices títulos, menús, etiquetas ni texto promocional. Une las frases seleccionadas con espacios. Si no hay ninguna frase válida, deja el campo 'summary' completamente vacío.\n"
            "7. EXTRACCIÓN DE METADATOS DE AUTOR Y FECHA (CRÍTICO):\n"
            "   - NO inventes el autor ('publication_author') ni la fecha ('publication_date').\n"
            "   - Si no hay un autor de carne y hueso visible en el texto o en metadata_detected, deja 'publication_author' vacío, o usa 'Redacción' únicamente si ese término exacto aparece explícitamente en el texto.\n"
            "   - Si 'metadata_detected' trae una fecha de publicación ('published_date') fiable, respétala a menos que haya una contradicción evidente en el texto del artículo.\n"
            "8. La justificación del score ('score_justification') debe explicar internamente de forma detallada por qué se ha asignado esta puntuación de relevancia o coincidencia. Este campo es puramente interno y NO se publica en WordPress.\n\n"
            "GUÍA DE PUNTUACIÓN ('match_score') APLICAR LA ESCALA COMPLETA:\n"
            "- 90-100: Reseña/Artículo centrado claramente en el libro.\n"
            "- 70-89: Artículo claramente relevante sobre el libro, aunque no sea una reseña pura.\n"
            "- 40-69: Artículo con tratamiento parcial del libro. Habla del libro durante una parte relevante, pero no es el tema único.\n"
            "- 20-39: Mención breve, lateral, adaptación, representación, comentario corto o referencia contextual útil.\n"
            "- 1-19: Mención muy débil pero real. Apenas unas líneas o frases descriptivas en el texto (ej. listado comentado), excluyendo bibliografía/catálogos automáticos.\n"
            "- 0: No menciona el libro, habla de otro libro, o el libro aparece solo en índice/bibliografía/catálogo automático/footer.\n"
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
        )
        if metadata_detected:
            user_content += (
                f"METADATOS PREVIAMENTE DETECTADOS DE FORMA DETERMINISTA (metadata_detected):\n"
                f"{json.dumps(metadata_detected, ensure_ascii=False, indent=2)}\n\n"
            )
        user_content += (
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
                raw_result = result_obj.model_dump()
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
                    "summary": "1 a 3 frases textuales extraídas de forma literal del cuerpo del artículo...",
                    "score_justification": "Explicación de la puntuación..."
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
                raw_result = {
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
                    "summary": str(data.get("summary", "")),
                    "score_justification": str(data.get("score_justification", ""))
                }
            except Exception as e_fallback:
                logger.error(f"OpenAI fallback query also failed: {e_fallback}")
                raise RuntimeError(f"error OpenAI: {str(e_fallback)}")

        # Literal summary quote verification and cleaning logic
        summary_val = raw_result.get("summary", "")
        verified_summary = ""
        
        if summary_val and article_text:
            import re
            # Split summary by sentences (supporting nested quotation marks / brackets)
            sentences = re.split(r'(?<=[.!?])\s+|(?<=[.!?]["\'«»“”‘’\)\]])\s+', summary_val)
            verified_sentences = []
            
            forbidden_words = ["score", "puntuación", "puntuacion", "porcentaje", "relevancia", "tratamiento parcial", "justifica", "criterio"]
            
            for s in sentences:
                s = s.strip()
                if not s:
                    continue
                # Clean prefix lists/bullet points (e.g. "1. ", "- ", "* ", "— ", "– ")
                s_clean = re.sub(r'^(\d+[\.\)]|-|\*|—|–)\s*', '', s).strip()
                # Clean quotes
                s_clean = s_clean.strip('"\'«»“”‘’').strip()
                if not s_clean:
                    continue
                    
                # Check for evaluation/forbidden words
                s_lower = s_clean.lower()
                has_forbidden = any(w in s_lower for w in forbidden_words)
                if has_forbidden:
                    logger.warning(f"Summary sentence skipped due to forbidden word: {s_clean}")
                    from app.services.logger_service import logger_service
                    logger_service.log(
                        level="WARNING",
                        action="OPENAI_SUMMARY_FORBIDDEN_WORDS",
                        message="Una frase del resumen de la IA contiene palabras de puntuación o score y fue omitida.",
                        isbn=isbn,
                        detail=json.dumps({
                            "original_sentence": s,
                            "forbidden_words_detected": [w for w in forbidden_words if w in s_lower]
                        }, ensure_ascii=False)
                    )
                    continue
                
                # Check if s_clean appears literally in article_text
                def norm(t: str) -> str:
                    return " ".join(t.split())
                
                if s_clean in article_text or norm(s_clean) in norm(article_text):
                    verified_sentences.append(s_clean)
                    if len(verified_sentences) >= 3:
                        break
                else:
                    logger.warning(f"Summary sentence skipped because it is not literal: {s_clean}")
            
            verified_summary = " ".join(verified_sentences).strip()
            
        raw_result["summary"] = verified_summary
        return raw_result

openai_analyzer = OpenAIAnalyzer()
