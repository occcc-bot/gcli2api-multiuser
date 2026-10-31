"""
数据模型（Pydantic v2）
- 聊天 API：OpenAI / Gemini / Claude 请求体解析模型
- 面板 API：登录、认证、凭证操作、配置保存模型
"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, model_validator


def model_to_dict(model: BaseModel) -> Dict[str, Any]:
    """Pydantic 模型 -> dict"""
    return model.model_dump(exclude_none=True)


# ==================== 模型列表 ====================

class Model(BaseModel):
    id: str
    object: str = "model"
    created: int = 0
    owned_by: str = "google"


class ModelList(BaseModel):
    data: List[Model]


# ==================== OpenAI 请求 ====================

class OpenAIToolFunction(BaseModel):
    name: str
    description: Optional[str] = None
    parameters: Optional[Dict[str, Any]] = None


class OpenAIToolCallFunction(BaseModel):
    """An assistant tool call carries arguments, not a tool parameter schema."""

    name: str
    arguments: Any = ""


class OpenAIToolCall(BaseModel):
    id: str
    type: Literal["function"] = "function"
    function: OpenAIToolCallFunction


class OpenAITool(BaseModel):
    type: Literal["function"] = "function"
    function: OpenAIToolFunction


class OpenAIChatMessage(BaseModel):
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: Any = None
    name: Optional[str] = None
    tool_calls: Optional[List[OpenAIToolCall]] = None
    tool_call_id: Optional[str] = None
    refusal: Optional[str] = None
    audio: Optional[Dict[str, Any]] = None
    function_call: Optional[Dict[str, Any]] = None

    @model_validator(mode="after")
    def validate_message(self):
        if self.tool_calls and self.role != "assistant":
            raise ValueError("only assistant messages may contain tool_calls")
        if self.role == "assistant" and self.audio is not None:
            raise ValueError("assistant audio history is unsupported by Antigravity")
        if self.function_call is not None:
            raise ValueError("legacy function_call history is unsupported; use tool_calls")
        if self.role in ("system", "developer") and not isinstance(self.content, str):
            if not isinstance(self.content, list) or not all(
                isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
                for part in self.content
            ):
                raise ValueError("system and developer messages require text content")
        return self


class OpenAIChatCompletionRequest(BaseModel):
    model: str
    messages: List[OpenAIChatMessage]
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    stream: bool = False
    tools: Optional[List[OpenAITool]] = None
    tool_choice: Optional[Any] = None
    stop: Optional[List[str]] = None
    frequency_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None
    user: Optional[str] = None


# ==================== Gemini 请求 ====================

class GeminiPart(BaseModel):
    model_config = ConfigDict(extra="allow")

    text: Optional[str] = None
    inlineData: Optional[Dict[str, Any]] = None
    inline_data: Optional[Dict[str, Any]] = None
    functionCall: Optional[Dict[str, Any]] = None
    functionResponse: Optional[Dict[str, Any]] = None
    fileData: Optional[Dict[str, Any]] = None
    thought: Optional[bool] = None


class GeminiContent(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Optional[str] = None
    parts: List[GeminiPart]


class GeminiSystemInstruction(BaseModel):
    parts: Optional[List[GeminiPart]] = None


class GeminiImageConfig(BaseModel):
    aspectRatio: Optional[str] = None
    imageSize: Optional[str] = None


class GeminiGenerationConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    temperature: Optional[float] = None
    topP: Optional[float] = None
    top_p: Optional[float] = None
    topK: Optional[float] = None
    top_k: Optional[float] = None
    maxOutputTokens: Optional[int] = None
    max_output_tokens: Optional[int] = None
    candidateCount: Optional[int] = None
    stopSequences: Optional[List[str]] = None
    stop_sequences: Optional[List[str]] = None
    responseMimeType: Optional[str] = None
    response_mime_type: Optional[str] = None
    thinkingConfig: Optional[Dict[str, Any]] = None
    responseModalities: Optional[List[str]] = None
    imageConfig: Optional[GeminiImageConfig] = None


class GeminiSafetySetting(BaseModel):
    category: Optional[str] = None
    threshold: Optional[str] = None


class GeminiRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    contents: List[GeminiContent]
    systemInstruction: Optional[GeminiSystemInstruction] = None
    system_instruction: Optional[GeminiSystemInstruction] = None
    generationConfig: Optional[GeminiGenerationConfig] = None
    generation_config: Optional[GeminiGenerationConfig] = None
    safetySettings: Optional[List[GeminiSafetySetting]] = None
    safety_settings: Optional[List[GeminiSafetySetting]] = None
    tools: Optional[List[Dict[str, Any]]] = None
    toolConfig: Optional[Dict[str, Any]] = None
    tool_config: Optional[Dict[str, Any]] = None
    sessionId: Optional[str] = None


# ==================== Claude 请求 ====================

class ClaudeContentBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str
    text: Optional[str] = None
    id: Optional[str] = None
    name: Optional[str] = None
    input: Optional[Dict[str, Any]] = None
    source: Optional[Dict[str, Any]] = None
    thinking: Optional[Dict[str, Any]] = None
    signature: Optional[str] = None


class ClaudeMessage(BaseModel):
    role: str
    content: Any


class ClaudeTool(BaseModel):
    name: str
    description: Optional[str] = None
    input_schema: Optional[Dict[str, Any]] = None


class ClaudeMetadata(BaseModel):
    user_id: Optional[str] = None


class ClaudeRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    messages: List[ClaudeMessage]
    system: Optional[Any] = None
    max_tokens: int = 8192
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    stream: bool = False
    stop_sequences: Optional[List[str]] = None
    tools: Optional[List[ClaudeTool]] = None
    tool_choice: Optional[Dict[str, Any]] = None
    metadata: Optional[ClaudeMetadata] = None
    thinking: Optional[Dict[str, Any]] = None


# ==================== 面板 API 模型 ====================

class LoginRequest(BaseModel):
    password: str


class AuthStartRequest(BaseModel):
    project_id: Optional[str] = None


class AuthCallbackRequest(BaseModel):
    project_id: Optional[str] = None


class AuthCallbackUrlRequest(BaseModel):
    callback_url: str
    project_id: Optional[str] = None


class CredFileActionRequest(BaseModel):
    email: str
    action: str  # enable, disable, delete


class CredFileBatchActionRequest(BaseModel):
    action: str  # "enable", "disable", "delete"
    emails: List[str]


class ConfigSaveRequest(BaseModel):
    config: dict
