import asyncio
import base64
import hashlib
import hmac
import importlib.metadata
import io
import json
import logging
import os
import re
import secrets
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from urllib.parse import quote, urlparse

import httpx
import numpy as np
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from gemini_webapi import GeminiClient, set_log_level
from gemini_webapi.constants import Model
from PIL import Image
from pydantic import BaseModel

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
set_log_level("INFO")

gemini_client = None
gemini_client_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
	"""Lazily initialize the Gemini client on first request and close it on shutdown."""
	try:
		yield
	finally:
		global gemini_client
		if gemini_client is not None:
			try:
				sync_cached_1psidts_from_client(gemini_client)
				await gemini_client.close()
			except Exception as e:
				logger.warning(f"Failed to close Gemini client during shutdown: {e}")
			finally:
				gemini_client = None


app = FastAPI(title="Gemini API FastAPI Server", lifespan=lifespan)


def get_gemini_webapi_version() -> str:
	"""Return the installed gemini-webapi package version for runtime diagnostics."""
	try:
		return importlib.metadata.version("gemini-webapi")
	except importlib.metadata.PackageNotFoundError:
		return "unknown"


def get_env_int(name: str, default: int, minimum: int = 0) -> int:
	"""Parse an integer environment variable with a lower bound."""
	raw = os.environ.get(name, "").strip()
	if not raw:
		return default
	try:
		return max(minimum, int(raw))
	except ValueError:
		logger.warning("Invalid integer for %s=%r, fallback=%s", name, raw, default)
		return default


def get_cached_1psidts_path(psid: str) -> str:
	"""Return the cache path for a rotated 1PSIDTS value."""
	if not psid or not re.match("^[\\w\\-\\.]+$", psid):
		return ""
	return os.path.join(GEMINI_COOKIE_PATH, f".cached_1psidts_{psid}.txt")


def load_cached_1psidts(psid: str) -> str:
	"""Load a cached rotated 1PSIDTS value for the given 1PSID."""
	cached_file_path = get_cached_1psidts_path(psid)
	if not cached_file_path:
		return ""

	if os.path.exists(cached_file_path):
		try:
			content = Path(cached_file_path).read_text().strip()
			if content:
				return content
		except Exception as e:
			logger.warning(f"Error reading cache file {cached_file_path}: {e}")

	return ""


def save_cached_1psidts(psid: str, psidts: str):
	"""Persist the latest rotated 1PSIDTS for the current 1PSID."""
	cached_file_path = get_cached_1psidts_path(psid)
	if not cached_file_path or not psidts:
		return

	try:
		os.makedirs(os.path.dirname(cached_file_path), exist_ok=True)
		current_value = ""
		if os.path.exists(cached_file_path):
			current_value = Path(cached_file_path).read_text().strip()
		if current_value == psidts:
			return
		Path(cached_file_path).write_text(psidts)
		try:
			os.chmod(cached_file_path, 0o600)
		except Exception:
			pass
	except Exception as e:
		logger.warning(f"Error writing cache file {cached_file_path}: {e}")


def sync_cached_1psidts_from_client(client: Optional[GeminiClient], psid: str = ""):
	"""Save the runtime 1PSIDTS from the live Gemini client into the legacy cache file."""
	if client is None:
		return
	runtime_psid = get_cookie_value(getattr(client, "cookies", None), "__Secure-1PSID") or psid or SECURE_1PSID
	runtime_psidts = get_cookie_value(getattr(client, "cookies", None), "__Secure-1PSIDTS")
	if runtime_psid and runtime_psidts:
		save_cached_1psidts(runtime_psid, runtime_psidts)


def get_cookie_value(cookies, name: str) -> str:
	"""Safely read a cookie value from an httpx cookie jar or mapping."""
	if not cookies:
		return ""

	for domain in (".google.com", ".googleusercontent.com", None):
		try:
			value = cookies.get(name, domain=domain) if domain is not None else cookies.get(name)
		except TypeError:
			value = cookies.get(name)
		except Exception:
			value = ""

		if value:
			return value

	return ""


# Add CORS middleware
app.add_middleware(
	CORSMiddleware,
	allow_origins=["*"],
	allow_credentials=True,
	allow_methods=["*"],
	allow_headers=["*"],
)

# Authentication credentials
SECURE_1PSID = os.environ.get("SECURE_1PSID", "")
SECURE_1PSIDTS = os.environ.get("SECURE_1PSIDTS", "")
API_KEY = os.environ.get("API_KEY", "")
ENABLE_THINKING = os.environ.get("ENABLE_THINKING", "false").lower() == "true"
TEMPORARY_CHAT = os.environ.get("TEMPORARY_CHAT", "false").lower() == "true"
AUTO_DELETE_CHAT = os.environ.get("AUTO_DELETE_CHAT", "false").lower() == "true" and not TEMPORARY_CHAT
GEMINI_MAX_CONCURRENT = get_env_int("GEMINI_MAX_CONCURRENT", 1, minimum=1)
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
SECRET_FILE_PATH = os.path.join(os.path.dirname(__file__), "secrets", "proxy_secret")
GEMINI_COOKIE_PATH = os.path.join(os.path.dirname(__file__), "secrets")
SESSION_VALIDATION_PROMPT = "Reply with exactly OK."
AUTH_FAILURE_TEXT_PATTERNS = (
	"are you signed in",
	"sign in",
	"signed in",
	"log in",
	"logged in",
)
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 Edg/144.0.0.0"

os.environ.setdefault("GEMINI_COOKIE_PATH", GEMINI_COOKIE_PATH)

gemini_generation_semaphore = None


async def background_delete_chat(client: GeminiClient, cid: str):
	"""Deletes a chat conversation in the background to avoid blocking the main thread."""
	if not cid:
		return
	try:
		await client.delete_chat(cid)
	except Exception as e:
		logger.error(f"Failed to auto-delete chat {cid}: {e}")


def response_indicates_auth_failure(text: str) -> bool:
	"""Return True if the response text looks like a signed-out or degraded session."""
	normalized = (text or "").strip().lower()
	if not normalized:
		return True
	return any(pattern in normalized for pattern in AUTH_FAILURE_TEXT_PATTERNS)


def get_gemini_generation_semaphore() -> asyncio.Semaphore:
	"""Return the shared semaphore that serializes Gemini generation requests."""
	global gemini_generation_semaphore
	if gemini_generation_semaphore is None:
		gemini_generation_semaphore = asyncio.Semaphore(GEMINI_MAX_CONCURRENT)
	return gemini_generation_semaphore


async def acquire_gemini_generation_slot(label: str) -> float:
	"""Acquire a shared Gemini slot and return the queue wait time in seconds."""
	start = time.monotonic()
	await get_gemini_generation_semaphore().acquire()
	waited = time.monotonic() - start
	if waited >= 0.2:
		logger.info("Gemini request queued: label=%s wait=%.2fs concurrency=%s", label, waited, GEMINI_MAX_CONCURRENT)
	return waited


def release_gemini_generation_slot():
	"""Release the shared Gemini generation slot."""
	get_gemini_generation_semaphore().release()


async def fetch_readable_chat_response(client: GeminiClient, cid: str, retry_delays: List[int]) -> Optional[object]:
	"""Poll Gemini history until the chat becomes readable or retries are exhausted."""
	for attempt, delay in enumerate(retry_delays, start=1):
		try:
			if delay:
				await asyncio.sleep(delay)

			recovered = await client.fetch_latest_chat_response(cid)
			if recovered and getattr(recovered, "text", ""):
				return recovered
		except Exception as e:
			logger.exception(
				"Gemini history read failed for cid=%s on retry %s/%s after %ss delay: %s",
				cid,
				attempt,
				len(retry_delays),
				delay,
				e,
			)
			continue

	return None


async def background_verify_chat_persistence(client: GeminiClient, cid: str, source: str):
	"""Best-effort verification that a returned cid is readable from Gemini history."""
	if not cid:
		return

	retry_delays = [1, 3, 8]
	recovered = await fetch_readable_chat_response(client, cid, retry_delays)
	if recovered:
		logger.debug(
			"Gemini history verification succeeded: source=%s cid=%s text_len=%s metadata=%s",
			source,
			cid,
			len(recovered.text),
			getattr(recovered, "metadata", None),
		)
		return

	logger.warning(
		"Gemini history verification exhausted retries for cid=%s source=%s",
		cid,
		source,
	)


async def validate_gemini_client_session(client: GeminiClient, source: str):
	"""Verify that an initialized client can create and read back a normal persistent Gemini chat."""
	validation_cid = None
	try:
		response = await client.generate_content(SESSION_VALIDATION_PROMPT, temporary=False)
		response_text = getattr(response, "text", "") or ""
		metadata = getattr(response, "metadata", None) or []
		validation_cid = metadata[0] if metadata else None

		if response_indicates_auth_failure(response_text):
			raise ValueError("validation probe returned signed-out or empty content")

		if not validation_cid:
			raise ValueError("validation probe returned no persistent chat metadata")

		recovered = await fetch_readable_chat_response(client, validation_cid, [1, 3, 8])
		if not recovered or response_indicates_auth_failure(getattr(recovered, "text", "") or ""):
			raise ValueError("validation probe chat was not readable from Gemini history")

		logger.info("Gemini session validation succeeded using %s credentials", source)
	finally:
		if validation_cid:
			try:
				await client.delete_chat(validation_cid)
			except Exception:
				logger.debug("Failed to delete Gemini validation chat %s", validation_cid)


def load_or_generate_secret() -> str:
	"""
	Load the signature secret from file, or generate a new one if not found.
	"""
	if os.path.exists(SECRET_FILE_PATH):
		try:
			with open(SECRET_FILE_PATH, "r") as f:
				secret = f.read().strip()
				if secret:
					logger.info(f"Loaded proxy secret from {SECRET_FILE_PATH}")
					return secret
		except Exception as e:
			logger.warning(f"Failed to read secret file, trying to generate a new one: {e}")

	# Generate new secret if not found or error occurred
	new_secret = secrets.token_hex(32)
	try:
		# Ensure directory exists
		os.makedirs(os.path.dirname(SECRET_FILE_PATH), exist_ok=True)
		with open(SECRET_FILE_PATH, "w") as f:
			f.write(new_secret)

		# Set restrictive permissions (user-only readable/writable)
		try:
			os.chmod(SECRET_FILE_PATH, 0o600)
		except Exception as e:
			logger.warning(f"Failed to set restrictive permissions on {SECRET_FILE_PATH}: {e}")

		logger.info(f"Generated new proxy secret and saved to {SECRET_FILE_PATH}")
		return new_secret
	except Exception as e:
		logger.error(f"Error writing secret file: {e}")
		# if unable to save, return an in-memory ephemeral secret instead of using API_KEY or SECURE_1PSID
		ephemeral_secret = secrets.token_urlsafe(32)
		logger.warning("Using an in-memory secret to proxy images for this session.")
		return ephemeral_secret


SIGNATURE_SECRET = load_or_generate_secret()

# Watermark removal constants
ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")
ALPHA_MAP_CACHE = {}


def get_alpha_map(size: int) -> np.ndarray:
	"""Load and cache the alpha map from the background capture image."""
	if size in ALPHA_MAP_CACHE:
		return ALPHA_MAP_CACHE[size]

	bg_path = os.path.join(ASSETS_DIR, f"bg_{size}.png")
	if not os.path.exists(bg_path):
		logger.warning(f"Watermark asset not found: {bg_path}")
		return None

	try:
		with Image.open(bg_path) as img:
			img_data = np.array(img.convert("RGB"))
			alpha_map = np.max(img_data, axis=2) / 255.0
			ALPHA_MAP_CACHE[size] = alpha_map
			return alpha_map
	except Exception as e:
		logger.error(f"Error loading alpha map {size}: {e}")
		return None


def remove_gemini_watermark(image_bytes: bytes) -> bytes:
	"""Remove Gemini watermark using Reverse Alpha Blending."""
	try:
		with Image.open(io.BytesIO(image_bytes)) as img:
			width, height = img.size
			orig_format = img.format

			if width > 1024 and height > 1024:
				logo_size, margin = 96, 64
			else:
				logo_size, margin = 48, 32

			alpha_map = get_alpha_map(logo_size)
			if alpha_map is None:
				return image_bytes

			x = width - margin - logo_size
			y = height - margin - logo_size
			if x < 0 or y < 0:
				logger.warning(f"Image too small for watermark removal: {width}x{height}")
				return image_bytes

			# Reverse Alpha Blending: original = (watermarked - α × 255) / (1 - α)
			img_array = np.array(img.convert("RGB")).astype(np.float64)
			roi = img_array[y : y + logo_size, x : x + logo_size].copy()

			alpha = np.clip(alpha_map, 0.002, 0.99)
			alpha_expanded = np.expand_dims(alpha, axis=2)
			cleaned_roi = (roi - alpha_expanded * 255.0) / (1.0 - alpha_expanded)
			cleaned_roi = np.clip(np.round(cleaned_roi), 0, 255).astype(np.uint8)

			img_array_uint8 = np.array(img.convert("RGB"))
			img_array_uint8[y : y + logo_size, x : x + logo_size] = cleaned_roi

			out_io = io.BytesIO()
			save_format = orig_format or "PNG"
			if save_format.upper() == "JPEG":
				Image.fromarray(img_array_uint8).save(out_io, format="JPEG", quality=95)
			else:
				Image.fromarray(img_array_uint8).save(out_io, format=save_format)
			return out_io.getvalue()

	except Exception as e:
		logger.error(f"Error removing watermark: {e}")
		return image_bytes


if not SECURE_1PSID or not SECURE_1PSIDTS:
	logger.warning("Gemini credentials are missing; set SECURE_1PSID and SECURE_1PSIDTS before serving requests.")
else:
	logger.info(
		"Startup config: thinking=%s temporary_chat=%s auto_delete_chat=%s max_concurrent=%s public_base_url=%s gemini_webapi=%s",
		ENABLE_THINKING,
		TEMPORARY_CHAT,
		AUTO_DELETE_CHAT,
		GEMINI_MAX_CONCURRENT,
		bool(PUBLIC_BASE_URL),
		get_gemini_webapi_version(),
	)
	if not re.match("^[\\w\\-\\.]+$", SECURE_1PSID):
		logger.warning(
			"SECURE_1PSID contains characters outside the safe cache filename pattern. This may be valid for auth, but cached 1PSIDTS lookup will fall back to the env value."
		)

if not API_KEY:
	logger.info("API key authentication is disabled.")
else:
	logger.info("API key authentication is enabled.")


def correct_markdown(md_text: str) -> str:
	"""
	修正Markdown文本，移除Google搜索链接包装器，并根据显示文本简化目标URL。
	"""

	def simplify_link_target(text_content: str) -> str:
		match_colon_num = re.match(r"([^:]+:\d+)", text_content)
		if match_colon_num:
			return match_colon_num.group(1)
		return text_content

	def replacer(match: re.Match) -> str:
		outer_open_paren = match.group(1)
		display_text = match.group(2)

		new_target_url = simplify_link_target(display_text)
		new_link_segment = f"[`{display_text}`]({new_target_url})"

		if outer_open_paren:
			return f"{outer_open_paren}{new_link_segment})"
		else:
			return new_link_segment

	pattern = r"(\()?\[`([^`]+?)`\]\((https://www.google.com/search\?q=)(.*?)(?<!\\)\)\)*(\))?"

	fixed_google_links = re.sub(pattern, replacer, md_text)
	# fix wrapped markdownlink
	pattern = r"`(\[[^\]]+\]\([^\)]+\))`"
	return re.sub(pattern, r"\1", fixed_google_links)


# Pydantic models for API requests and responses
class ContentItem(BaseModel):
	type: str
	text: Optional[str] = None
	image_url: Optional[Dict[str, str]] = None


class Message(BaseModel):
	role: str
	content: Union[str, List[ContentItem]]
	name: Optional[str] = None


class ChatCompletionRequest(BaseModel):
	model: str
	messages: List[Message]
	temperature: Optional[float] = 0.7
	top_p: Optional[float] = 1.0
	n: Optional[int] = 1
	stream: Optional[bool] = False
	max_tokens: Optional[int] = None
	presence_penalty: Optional[float] = 0
	frequency_penalty: Optional[float] = 0
	user: Optional[str] = None


class Choice(BaseModel):
	index: int
	message: Message
	finish_reason: str


class Usage(BaseModel):
	prompt_tokens: int
	completion_tokens: int
	total_tokens: int


class ChatCompletionResponse(BaseModel):
	id: str
	object: str = "chat.completion"
	created: int
	model: str
	choices: List[Choice]
	usage: Usage


class ModelData(BaseModel):
	id: str
	object: str = "model"
	created: int
	owned_by: str = "google"


class ModelList(BaseModel):
	object: str = "list"
	data: List[ModelData]


def get_runtime_available_models(client: Optional[GeminiClient]) -> List[Any]:
	"""Return account-available Gemini models discovered at runtime."""
	if client is None:
		return []
	try:
		models = client.list_models()
	except Exception as e:
		logger.warning(f"Failed to inspect runtime model list: {e}")
		return []

	if not models:
		return []

	result = []
	for model in models:
		model_name = getattr(model, "model_name", "") or ""
		if not model_name:
			continue
		if getattr(model, "is_available", True) is False:
			continue
		result.append(model)
	return result


def serialize_runtime_model(model: Any, now: int) -> Dict[str, Any]:
	"""Convert a runtime AvailableModel into OpenAI-compatible model metadata."""
	return {
		"id": getattr(model, "model_name", ""),
		"object": "model",
		"created": now,
		"owned_by": "google-gemini-web",
		"display_name": getattr(model, "display_name", "") or getattr(model, "model_name", ""),
		"description": getattr(model, "description", "") or "",
		"advanced_only": bool(getattr(model, "advanced_only", False)),
	}


def score_runtime_model_match(model: Any, requested: str) -> int:
	"""Best-effort score for mapping an OpenAI-style model name onto runtime Gemini models."""
	requested = requested.lower().strip()
	if not requested:
		return -1

	model_name = str(getattr(model, "model_name", "") or "").lower()
	display_name = str(getattr(model, "display_name", "") or "").lower()
	description = str(getattr(model, "description", "") or "").lower()
	haystack = " ".join(part for part in [model_name, display_name, description] if part)

	score = 0
	if requested == model_name:
		score += 1000
	if requested == display_name:
		score += 900
	if requested in model_name:
		score += 600
	if requested in display_name:
		score += 500
	if requested in haystack:
		score += 300

	token_groups = [
		(["thinking", "reasoning"], 160),
		(["flash", "fast"], 140),
		(["pro"], 140),
		(["vision", "image"], 100),
		(["video", "veo"], 100),
		(["audio", "music"], 80),
	]
	for aliases, weight in token_groups:
		if any(alias in requested for alias in aliases) and any(alias in haystack for alias in aliases):
			score += weight

	requested_tokens = [token for token in re.split(r"[^a-z0-9]+", requested) if token]
	for token in requested_tokens:
		if token in model_name:
			score += 40
		elif token in display_name:
			score += 25
		elif token in description:
			score += 10

	return score


# Authentication dependency
async def verify_api_key(authorization: str = Header(None)):
	"""
	Verify the API key extracted from the Authorization header.

	Raises:
		HTTPException: If the authorization header is missing, incorrectly formatted, or the token is invalid.
	"""
	if not API_KEY:
		# If API_KEY is not set in environment, skip validation (for development)
		return

	if not authorization:
		raise HTTPException(status_code=401, detail="Missing Authorization header")

	try:
		scheme, token = authorization.split()
		if scheme.lower() != "bearer":
			raise HTTPException(
				status_code=401,
				detail="Invalid authentication scheme. Use Bearer token",
			)

		if token != API_KEY:
			raise HTTPException(status_code=401, detail="Invalid API key")
	except ValueError:
		raise HTTPException(
			status_code=401,
			detail="Invalid authorization format. Use 'Bearer YOUR_API_KEY'",
		)

	return token


# Simple error handler middleware
@app.middleware("http")
async def error_handling(request: Request, call_next):
	"""
	Global middleware to catch unhandled exceptions, log the error,
	and return a standardized HTTP 500 response.
	"""
	try:
		return await call_next(request)
	except Exception:
		logger.exception("Request failed")
		return JSONResponse(
			status_code=500,
			content={
				"error": {
					"message": "Internal server error",
					"type": "internal_server_error",
				}
			},
		)


# Get list of available models
@app.get("/v1/models")
async def list_models():
	"""Return models discovered for the current account, falling back to static enum values."""
	now = int(datetime.now(tz=timezone.utc).timestamp())
	data: List[Dict[str, Any]] = []
	try:
		client = await get_gemini_client()
		runtime_models = get_runtime_available_models(client)
		if runtime_models:
			data = [serialize_runtime_model(model, now) for model in runtime_models]
	except Exception as e:
		logger.warning(f"Falling back to static model list: {e}")

	if not data:
		data = [
			{
				"id": m.model_name,  # 如 "gemini-2.0-flash"
				"object": "model",
				"created": now,
				"owned_by": "google-gemini-web",
			}
			for m in Model
			if m is not Model.UNSPECIFIED
		]
	return {"object": "list", "data": data}


# Helper to convert between Gemini and OpenAI model names
def map_model_name(openai_model_name: str, runtime_models: Optional[List[Any]] = None) -> Union[Model, Any]:
	"""Map an OpenAI-style model name onto a Gemini enum/runtime model."""
	normalized_openai_model_name = (openai_model_name or "").lower().strip()

	if runtime_models:
		best_model = None
		best_score = -1
		for runtime_model in runtime_models:
			score = score_runtime_model_match(runtime_model, normalized_openai_model_name)
			if score > best_score:
				best_score = score
				best_model = runtime_model
		if best_model is not None and best_score > 0:
			return best_model

	# 首先尝试直接查找匹配的模型名称
	for m in Model:
		model_name = m.model_name if hasattr(m, "model_name") else str(m)
		if normalized_openai_model_name in model_name.lower():
			return m

	# 如果找不到匹配项，使用默认映射
	model_keywords = {
		"gemini-pro": ["pro", "2.0"],
		"gemini-pro-vision": ["vision", "pro"],
		"gemini-flash": ["flash", "2.0"],
		"gemini-1.5-pro": ["1.5", "pro"],
		"gemini-1.5-flash": ["1.5", "flash"],
	}

	# 根据关键词模糊匹配
	keywords = None
	for key, candidate_keywords in model_keywords.items():
		normalized_key = key.lower()
		matches_key = normalized_key in normalized_openai_model_name
		matches_any_kw = any(kw.lower() in normalized_openai_model_name for kw in candidate_keywords)
		if matches_key or matches_any_kw:
			keywords = candidate_keywords
			break

	if keywords is None:
		if "flash" in normalized_openai_model_name:
			keywords = ["flash"]
		elif "vision" in normalized_openai_model_name:
			keywords = ["vision"]
		else:
			keywords = ["pro"]

	for m in Model:
		model_name = m.model_name if hasattr(m, "model_name") else str(m)
		if all(kw.lower() in model_name.lower() for kw in keywords):
			return m

	# 如果还是找不到，返回第一个模型
	return next(iter(Model))


# Prepare conversation history from OpenAI messages format
def prepare_conversation(messages: List[Message]) -> tuple:
	"""
	Convert a list of OpenAI-formatted message objects into a
	flat string conversation format suitable for the Gemini API.
	Also extracts and saves base64 images to temporary files.

	Returns:
		A tuple containing the constructed conversation string and a list of paths to temporary image files.
	"""
	conversation = ""
	temp_files = []

	for msg in messages:
		if isinstance(msg.content, str):
			# String content handling
			if msg.role == "system":
				conversation += f"System: {msg.content}\n\n"
			elif msg.role == "user":
				conversation += f"Human: {msg.content}\n\n"
			elif msg.role == "assistant":
				conversation += f"Assistant: {msg.content}\n\n"
		else:
			# Mixed content handling
			if msg.role == "user":
				conversation += "Human: "
			elif msg.role == "system":
				conversation += "System: "
			elif msg.role == "assistant":
				conversation += "Assistant: "

			for item in msg.content:
				if item.type == "text":
					conversation += item.text or ""
				elif item.type == "image_url" and item.image_url:
					# Handle image
					image_url = item.image_url.get("url", "")
					if image_url.startswith("data:image/"):
						# Process base64 encoded image
						try:
							# Extract the base64 part
							base64_data = image_url.split(",")[1]
							image_data = base64.b64decode(base64_data)

							# Create temporary file to hold the image
							with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
								tmp.write(image_data)
								temp_files.append(tmp.name)
						except Exception as e:
							logger.error(f"Error processing base64 image: {str(e)}")

			conversation += "\n\n"

	# Add a final prompt for the assistant to respond to
	conversation += "Assistant: "

	return conversation, temp_files


# Dependency to get the initialized Gemini client
async def get_gemini_client():
	"""
	Get or initialize the global GeminiClient instance.

	Raises:
		HTTPException: If initialization fails due to invalid parameters or connection issues.
	"""
	global gemini_client
	if gemini_client is not None:
		return gemini_client

	async with gemini_client_lock:
		if gemini_client is not None:
			return gemini_client

		try:
			psid = SECURE_1PSID
			cached_psidts = load_cached_1psidts(psid)
			attempts = []

			if cached_psidts:
				attempts.append(("cache", cached_psidts))
			if SECURE_1PSIDTS:
				attempts.append(("environment", SECURE_1PSIDTS))

			seen_psidts = set()
			new_attempts = []
			for source, psidts in attempts:
				if not psidts or psidts in seen_psidts:
					continue
				seen_psidts.add(psidts)
				new_attempts.append((source, psidts))
			attempts = new_attempts

			if not attempts:
				raise HTTPException(
					status_code=500,
					detail="Missing SECURE_1PSIDTS and no cached rotated 1PSIDTS is available",
				)

			last_error = None
			for source, psidts in attempts:
				tmp_client = None
				try:
					logger.info("Initializing Gemini client using %s credentials", source)

					tmp_client = GeminiClient(psid, psidts)
					await tmp_client.init(timeout=300, auto_refresh=True, refresh_interval=600)
					sync_cached_1psidts_from_client(tmp_client, psid)

					gemini_client = tmp_client
					break
				except Exception as e:
					last_error = e
					logger.warning(f"Gemini session setup failed using {source} 1PSIDTS: {e}")
					if tmp_client is not None:
						try:
							await tmp_client.close()
						except Exception:
							pass

			if gemini_client is None:
				raise last_error

		except HTTPException:
			raise
		except Exception as e:
			logger.error(f"Failed to initialize Gemini client: {str(e)}")
			raise HTTPException(status_code=500, detail=f"Failed to initialize Gemini client: {str(e)}")
	return gemini_client


def get_image_signature(url: str) -> str:
	"""
	Generate a HMAC-SHA256 signature for the image URL using the persistent SIGNATURE_SECRET.
	"""
	secret = SIGNATURE_SECRET.encode()
	return hmac.new(secret, url.encode(), hashlib.sha256).hexdigest()


def postprocess_text(text: str) -> str:
	"""Apply text cleanup and markdown corrections to response text."""
	text = text.replace("&lt;", "<").replace("\\<", "<").replace("\\_", "_").replace("\\>", ">")
	return correct_markdown(text)


def get_signed_proxy_url(base_url: str, path: str, url: str) -> str:
	"""Build a signed proxy URL for Google-hosted assets."""
	sig = get_image_signature(url)
	return f"{base_url}{path}?url={quote(url)}&sig={sig}"


def extract_image_markdown(response, base_url: str) -> str:
	"""Extract images from a response and return markdown image links."""
	result = ""
	if hasattr(response, "images") and response.images:
		for img in response.images:
			img_url = getattr(img, "url", None)
			if img_url:
				proxy_url = get_signed_proxy_url(base_url, "/gemini-proxy/image", img_url)
				result += f"\n\n![🎨 Loading image...]({proxy_url})"
	return result


def extract_video_markdown(response, base_url: str) -> str:
	"""Extract generated videos and return markdown links."""
	result = ""
	if hasattr(response, "videos") and response.videos:
		for index, video in enumerate(response.videos, start=1):
			video_url = getattr(video, "url", None)
			if video_url:
				proxy_url = get_signed_proxy_url(base_url, "/gemini-proxy/media", video_url)
				result += f"\n\n[🎬 Video {index}]({proxy_url})"
	return result


def extract_media_markdown(response, base_url: str) -> str:
	"""Extract generated audio/video media and return markdown links."""
	result = ""
	if hasattr(response, "media") and response.media:
		for index, media in enumerate(response.media, start=1):
			mp4_url = getattr(media, "url", None) or getattr(media, "mp4_url", None)
			mp3_url = getattr(media, "mp3_url", None)
			if mp4_url:
				proxy_url = get_signed_proxy_url(base_url, "/gemini-proxy/media", mp4_url)
				result += f"\n\n[🎥 Media Video {index}]({proxy_url})"
			if mp3_url:
				proxy_url = get_signed_proxy_url(base_url, "/gemini-proxy/media", mp3_url)
				result += f"\n\n[🎵 Media Audio {index}]({proxy_url})"
	return result


def extract_asset_markdown(response, base_url: str) -> str:
	"""Collect all non-text Gemini assets into markdown links."""
	return (
		extract_image_markdown(response, base_url)
		+ extract_video_markdown(response, base_url)
		+ extract_media_markdown(response, base_url)
	)


@app.post("/v1/chat/completions")
async def create_chat_completion(
	request: ChatCompletionRequest,
	raw_request: Request,
	api_key: str = Depends(verify_api_key),
):
	"""
	Handle chat completion requests, translating from OpenAI API format to Gemini API format.
	Supports both streaming and non-streaming responses, caching, thinking features,
	and background conversation cleanup based on configuration.
	"""
	try:
		global gemini_client
		gemini_client = await get_gemini_client()
		runtime_models = get_runtime_available_models(gemini_client)

		conversation, temp_files = prepare_conversation(request.messages)
		logger.info(
			"Chat completion request: stream=%s requested_model=%s messages=%s temp_files=%s runtime_models=%s",
			request.stream,
			request.model,
			len(request.messages),
			len(temp_files),
			len(runtime_models),
		)

		model = map_model_name(request.model, runtime_models=runtime_models)
		resolved_model_name = getattr(model, "model_name", None) or str(model)
		logger.info("Resolved requested model '%s' -> '%s'", request.model, resolved_model_name)

		completion_id = f"chatcmpl-{uuid.uuid4()}"
		created_time = int(time.time())
		base_url = PUBLIC_BASE_URL or str(raw_request.base_url).rstrip("/")

		gen_kwargs = {"model": model}
		if TEMPORARY_CHAT:
			gen_kwargs["temporary"] = True
		if temp_files:
			gen_kwargs["files"] = temp_files

		if request.stream:
			async def generate_stream():
				captured_cid = None
				slot_acquired = False
				try:
					def make_chunk(delta: dict, finish_reason=None):
						return (
							"data: "
							+ json.dumps(
								{
									"id": completion_id,
									"object": "chat.completion.chunk",
									"created": created_time,
									"model": request.model,
									"choices": [
										{
											"index": 0,
											"delta": delta,
											"finish_reason": finish_reason,
										}
									],
								}
							)
							+ "\n\n"
						)

					yield make_chunk({"role": "assistant"})
					waited = await acquire_gemini_generation_slot("stream")
					slot_acquired = True
					if waited >= 0.2:
						logger.info("Streaming request waited %.2fs before entering Gemini lane", waited)

					thinking_started = False
					thinking_ended = False
					yielded_images = 0
					yielded_videos = 0
					yielded_media = 0
					text_buffer = ""
					chunk_count = 0
					last_metadata = None

					async for chunk in gemini_client.generate_content_stream(conversation, **gen_kwargs):
						chunk_count += 1
						if hasattr(chunk, "metadata") and chunk.metadata:
							last_metadata = chunk.metadata
						if AUTO_DELETE_CHAT and captured_cid is None and hasattr(chunk, "metadata") and chunk.metadata:
							captured_cid = chunk.metadata[0]

						if ENABLE_THINKING and hasattr(chunk, "thoughts_delta") and chunk.thoughts_delta:
							if not thinking_started:
								yield make_chunk({"content": "<think>\n"})
								thinking_started = True
							yield make_chunk(
								{
									"content": chunk.thoughts_delta,
									"reasoning_content": chunk.thoughts_delta,
								}
							)

						if hasattr(chunk, "text_delta") and chunk.text_delta:
							if thinking_started and not thinking_ended:
								thinking_ended = True
								yield make_chunk({"content": "\n</think>\n\n"})

							text_buffer += chunk.text_delta
							safe_to_yield = False
							if (
								text_buffer[-1].isspace()
								and text_buffer.count("[") == text_buffer.count("]")
								and text_buffer.count("(") == text_buffer.count(")")
							):
								safe_to_yield = True
							elif len(text_buffer) > 500:
								safe_to_yield = True

							if safe_to_yield:
								yield make_chunk({"content": postprocess_text(text_buffer)})
								text_buffer = ""

						if hasattr(chunk, "images") and chunk.images and len(chunk.images) > yielded_images:
							if thinking_started and not thinking_ended:
								thinking_ended = True
								yield make_chunk({"content": "\n</think>\n\n"})

							for img in chunk.images[yielded_images:]:
								img_url = getattr(img, "url", None)
								if img_url:
									proxy_url = get_signed_proxy_url(base_url, "/gemini-proxy/image", img_url)
									yield make_chunk({"content": f"\n\n![🎨 Loading image...]({proxy_url})\n\n"})
							yielded_images = len(chunk.images)

						if hasattr(chunk, "videos") and chunk.videos and len(chunk.videos) > yielded_videos:
							for idx, video in enumerate(chunk.videos[yielded_videos:], start=yielded_videos + 1):
								video_url = getattr(video, "url", None)
								if video_url:
									proxy_url = get_signed_proxy_url(base_url, "/gemini-proxy/media", video_url)
									yield make_chunk({"content": f"\n\n[🎬 Video {idx}]({proxy_url})\n\n"})
							yielded_videos = len(chunk.videos)

						if hasattr(chunk, "media") and chunk.media and len(chunk.media) > yielded_media:
							for idx, media in enumerate(chunk.media[yielded_media:], start=yielded_media + 1):
								mp4_url = getattr(media, "url", None) or getattr(media, "mp4_url", None)
								mp3_url = getattr(media, "mp3_url", None)
								if mp4_url:
									proxy_url = get_signed_proxy_url(base_url, "/gemini-proxy/media", mp4_url)
									yield make_chunk({"content": f"\n\n[🎥 Media Video {idx}]({proxy_url})\n\n"})
								if mp3_url:
									proxy_url = get_signed_proxy_url(base_url, "/gemini-proxy/media", mp3_url)
									yield make_chunk({"content": f"\n\n[🎵 Media Audio {idx}]({proxy_url})\n\n"})
							yielded_media = len(chunk.media)

					if text_buffer:
						yield make_chunk({"content": postprocess_text(text_buffer)})
					if thinking_started and not thinking_ended:
						yield make_chunk({"content": "\n</think>\n\n"})

					yield make_chunk({}, finish_reason="stop")
					yield "data: [DONE]\n\n"

					logger.info(
						"Streaming response completed: chunks=%s images=%s videos=%s media=%s",
						chunk_count,
						yielded_images,
						yielded_videos,
						yielded_media,
					)
				except HTTPException:
					raise
				except Exception as e:
					logger.error(f"Error during streaming: {str(e)}", exc_info=True)
					error_msg = "\n\n[An internal error occurred while streaming]"
					yield make_chunk({"content": error_msg})
					yield make_chunk({}, finish_reason="stop")
					yield "data: [DONE]\n\n"
				finally:
					if slot_acquired:
						release_gemini_generation_slot()
					sync_cached_1psidts_from_client(gemini_client)
					if AUTO_DELETE_CHAT and captured_cid:
						asyncio.create_task(background_delete_chat(gemini_client, captured_cid))
					for temp_file in temp_files:
						try:
							os.unlink(temp_file)
						except Exception as e:
							logger.warning(f"Failed to delete temp file {temp_file}: {str(e)}")

			return StreamingResponse(generate_stream(), media_type="text/event-stream")

		slot_acquired = False
		try:
			waited = await acquire_gemini_generation_slot("non-stream")
			slot_acquired = True
			if waited >= 0.2:
				logger.info("Non-stream request waited %.2fs before entering Gemini lane", waited)
			response = await gemini_client.generate_content(conversation, **gen_kwargs)
			if AUTO_DELETE_CHAT and hasattr(response, "metadata") and response.metadata and len(response.metadata) > 0:
				cid = response.metadata[0]
				asyncio.create_task(background_delete_chat(gemini_client, cid))
			elif not getattr(response, "metadata", None):
				logger.warning("Non-stream response returned no Gemini metadata. This request may not map to a persistent Gemini chat.")
		finally:
			if slot_acquired:
				release_gemini_generation_slot()
			sync_cached_1psidts_from_client(gemini_client)
			for temp_file in temp_files:
				try:
					os.unlink(temp_file)
				except Exception as e:
					logger.warning(f"Failed to delete temp file {temp_file}: {str(e)}")

		reply_text = ""
		if ENABLE_THINKING and hasattr(response, "thoughts") and response.thoughts:
			reply_text += f"<think>\n{response.thoughts}\n</think>\n\n"
		if hasattr(response, "text"):
			reply_text += response.text
		else:
			reply_text += str(response)

		reply_text += extract_asset_markdown(response, base_url)
		reply_text = postprocess_text(reply_text)

		if not reply_text or reply_text.strip() == "":
			logger.warning("Empty response received from Gemini")
			reply_text = "Server returned an empty response. Please check that Gemini API credentials are valid."

		result = {
			"id": completion_id,
			"object": "chat.completion",
			"created": created_time,
			"model": request.model,
			"choices": [
				{
					"index": 0,
					"message": {"role": "assistant", "content": reply_text},
					"finish_reason": "stop",
				}
			],
			"usage": {
				"prompt_tokens": len(conversation.split()),
				"completion_tokens": len(reply_text.split()),
				"total_tokens": len(conversation.split()) + len(reply_text.split()),
			},
		}

		logger.info("Non-streaming response completed")
		return result

	except HTTPException:
		raise
	except Exception as e:
		logger.error(f"Error generating completion: {str(e)}", exc_info=True)
		raise HTTPException(status_code=500, detail=f"Error generating completion: {str(e)}")


ALLOWED_PROXY_DOMAINS = ["google.com", "googleusercontent.com", "gstatic.com"]
IMAGE_PROXY_MAX_BYTES = 10 * 1024 * 1024
MEDIA_PROXY_MAX_BYTES = 100 * 1024 * 1024


def verify_proxy_signature(url: str, sig: str):
	expected_sig = get_image_signature(url)
	if not hmac.compare_digest(sig, expected_sig):
		logger.warning(f"Invalid signature for proxy request: {url}")
		raise HTTPException(status_code=403, detail="Invalid signature")


def validate_proxy_target(url: str):
	try:
		parsed = urlparse(url)
		if parsed.scheme not in ["http", "https"]:
			logger.warning(f"Invalid scheme in proxy request: {parsed.scheme}")
			raise HTTPException(status_code=400, detail="Invalid URL scheme")

		hostname = parsed.hostname
		if not hostname:
			logger.warning(f"No hostname in proxy request: {url}")
			raise HTTPException(status_code=400, detail="Invalid URL")

		hostname = hostname.lower()
		is_allowed = any(hostname == d or hostname.endswith("." + d) for d in ALLOWED_PROXY_DOMAINS)
		if not is_allowed:
			logger.warning(f"Blocked proxy request for domain: {hostname}")
			raise HTTPException(status_code=403, detail="Domain not allowed")
	except ValueError:
		logger.warning(f"Malformed URL in proxy request: {url}")
		raise HTTPException(status_code=400, detail="Invalid URL")


def build_proxy_cookie_jar() -> httpx.Cookies:
	jar = httpx.Cookies()
	psid = SECURE_1PSID
	psidts = get_cookie_value(getattr(gemini_client, "cookies", None), "__Secure-1PSIDTS") or load_cached_1psidts(psid) or SECURE_1PSIDTS
	jar.set("__Secure-1PSID", psid, domain=".google.com")
	jar.set("__Secure-1PSIDTS", psidts, domain=".google.com")
	jar.set("__Secure-1PSID", psid, domain=".googleusercontent.com")
	jar.set("__Secure-1PSIDTS", psidts, domain=".googleusercontent.com")
	return jar


def build_proxy_headers(accept: str) -> Dict[str, str]:
	return {
		"User-Agent": DEFAULT_USER_AGENT,
		"Accept": accept,
		"Accept-Language": "en-US,en;q=0.9",
		"Referer": "https://gemini.google.com/",
	}


async def proxy_google_asset(
	url: str,
	*,
	accept: str,
	timeout: float,
	max_bytes: int,
	default_media_type: str,
	ensure_full_size_image: bool = False,
	remove_watermark: bool = False,
) -> Response:
	validate_proxy_target(url)
	headers = build_proxy_headers(accept)
	jar = build_proxy_cookie_jar()
	fetch_url = url
	if ensure_full_size_image:
		fetch_url = re.sub(r"=s\d+$", "=s0", url) if re.search(r"=s\d+$", url) else url + "=s0"

	async with httpx.AsyncClient(http2=True, cookies=jar, follow_redirects=True) as client:
		try:
			async with client.stream("GET", fetch_url, timeout=timeout, headers=headers) as resp:
				if resp.status_code != 200:
					logger.error(f"Google returned {resp.status_code} for asset: {url}")
				resp.raise_for_status()

				content = bytearray()
				async for chunk in resp.aiter_bytes():
					content.extend(chunk)
					if len(content) > max_bytes:
						logger.warning(f"Asset too large: {url} (exceeded {max_bytes} bytes)")
						raise HTTPException(status_code=413, detail="Asset too large")

				upstream_content_type = resp.headers.get("content-type", default_media_type).lower()
				if remove_watermark:
					if not upstream_content_type.startswith("image/"):
						logger.warning(f"Rejected non-image Content-Type: {upstream_content_type} for {url}")
						media_type = default_media_type
					else:
						media_type = upstream_content_type
				else:
					if upstream_content_type.startswith(("video/", "audio/", "image/")):
						media_type = upstream_content_type
					else:
						logger.warning(f"Unexpected media Content-Type: {upstream_content_type} for {url}")
						media_type = default_media_type

				processed_content = bytes(content)
				if remove_watermark and media_type in ["image/png", "image/jpeg", "image/webp"]:
					processed_content = remove_gemini_watermark(processed_content)

				return Response(
					content=processed_content,
					media_type=media_type,
					headers={
						"Cross-Origin-Resource-Policy": "cross-origin",
						"Access-Control-Allow-Origin": "*",
						"Cache-Control": "public, max-age=86400",
						"X-Content-Type-Options": "nosniff",
					},
				)
		except httpx.HTTPStatusError as e:
			logger.error(f"Failed to fetch asset: {e.response.status_code} for {url}")
			raise HTTPException(
				status_code=e.response.status_code,
				detail=f"Failed to fetch asset: Google returned {e.response.status_code}",
			)
		except HTTPException:
			raise
		except Exception as e:
			logger.error(f"Proxy error: {str(e)}")
			raise HTTPException(status_code=500, detail="Internal proxy error")


async def stream_google_media_asset(
	url: str,
	*,
	accept: str,
	timeout: float,
	max_bytes: int,
	default_media_type: str,
) -> StreamingResponse:
	"""Stream media assets without buffering the entire file in memory."""
	validate_proxy_target(url)
	headers = build_proxy_headers(accept)
	jar = build_proxy_cookie_jar()
	client = httpx.AsyncClient(http2=True, cookies=jar, follow_redirects=True)

	try:
		request = client.build_request("GET", url, headers=headers)
		resp = await client.send(request, stream=True)
		if resp.status_code != 200:
			logger.error(f"Google returned {resp.status_code} for media asset: {url}")
		resp.raise_for_status()

		upstream_content_type = resp.headers.get("content-type", default_media_type).lower()
		if upstream_content_type.startswith(("video/", "audio/", "image/")):
			media_type = upstream_content_type
		else:
			logger.warning(f"Unexpected media Content-Type: {upstream_content_type} for {url}")
			media_type = default_media_type

		content_length_header = resp.headers.get("content-length")
		content_length = None
		if content_length_header:
			try:
				content_length = int(content_length_header)
			except ValueError:
				content_length = None
		if content_length is not None and content_length > max_bytes:
			await resp.aclose()
			await client.aclose()
			logger.warning(f"Media asset too large by content-length: {url} ({content_length}>{max_bytes})")
			raise HTTPException(status_code=413, detail="Asset too large")

		response_headers = {
			"Cross-Origin-Resource-Policy": "cross-origin",
			"Access-Control-Allow-Origin": "*",
			"Cache-Control": "public, max-age=86400",
			"X-Content-Type-Options": "nosniff",
		}
		if content_length is not None:
			response_headers["Content-Length"] = str(content_length)

		async def iter_bytes():
			total = 0
			try:
				async for chunk in resp.aiter_bytes():
					total += len(chunk)
					if total > max_bytes:
						logger.warning(f"Media asset exceeded streaming limit: {url} ({total}>{max_bytes})")
						break
					yield chunk
			finally:
				await resp.aclose()
				await client.aclose()

		return StreamingResponse(iter_bytes(), media_type=media_type, headers=response_headers)
	except httpx.HTTPStatusError as e:
		await client.aclose()
		logger.error(f"Failed to fetch media asset: {e.response.status_code} for {url}")
		raise HTTPException(
			status_code=e.response.status_code,
			detail=f"Failed to fetch asset: Google returned {e.response.status_code}",
		)
	except HTTPException:
		await client.aclose()
		raise
	except Exception as e:
		await client.aclose()
		logger.error(f"Media proxy error: {str(e)}")
		raise HTTPException(status_code=500, detail="Internal proxy error")


@app.get("/gemini-proxy/image")
async def proxy_image(url: str, sig: str):
	"""Proxy images from Google domains and remove Gemini watermark when possible."""
	verify_proxy_signature(url, sig)
	return await proxy_google_asset(
		url,
		accept="image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
		timeout=15.0,
		max_bytes=IMAGE_PROXY_MAX_BYTES,
		default_media_type="image/png",
		ensure_full_size_image=True,
		remove_watermark=True,
	)


@app.get("/gemini-proxy/media")
async def proxy_media(url: str, sig: str):
	"""Proxy Google-hosted audio/video assets using authenticated Gemini cookies."""
	verify_proxy_signature(url, sig)
	return await stream_google_media_asset(
		url,
		accept="video/*,audio/*,application/octet-stream;q=0.9,*/*;q=0.5",
		timeout=30.0,
		max_bytes=MEDIA_PROXY_MAX_BYTES,
		default_media_type="application/octet-stream",
	)


@app.get("/")
async def root():
	"""
	Health check endpoint to verify the API server is currently running.
	"""
	return {"status": "online", "message": "Gemini API FastAPI Server is running"}


if __name__ == "__main__":
	import uvicorn

	uvicorn.run("main:app", host="0.0.0.0", port=8000, log_level="info")
