import os
import uvicorn

if __name__ == "__main__":
    # Respect environment variables for runner/packager
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    # Run the FastAPI app defined in app.py
    uvicorn.run("app:app", host=host, port=port, log_level="info")
