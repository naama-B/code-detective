# Checks that everything is connected: the Gemini key, the LangSmith key, and the packages.
from dotenv import load_dotenv
from langchain.chat_models import init_chat_model

# Model name. If you get a "model not found" error, copy an exact name from AI Studio.
MODEL_NAME = "gemini-3.5-flash-lite"

load_dotenv()  # Loads the keys from the .env file

llm = init_chat_model(MODEL_NAME, model_provider="google_genai")
response = llm.invoke("Say hello in one sentence")
print(response.text)  # .text returns only the text, without the technical parts
