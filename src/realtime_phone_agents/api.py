from uuid import uuid4

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from realtime_phone_agents.agent.fastrtc_agent import FastRTCAgent

app = FastAPI(
    title="Phone Calling Agent API",
    description="An AI-powered phone calling agent API using FastRTC",
    docs_url="/docs",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Frontend URL
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health_check():
    """Health check endpoint to monitor service readiness."""
    try:
        return {
            "status": "healthy",
            "message": "Service is ready",
        }
    except Exception as e:
        return {
            "status": "unhealthy",
            "message": f"Service initialization failed: {str(e)}",
        }


agent = FastRTCAgent(
    thread_id=str(uuid4()),
)

# Mount Websocket endpoint for Twilio Integration
agent.stream.mount(app, path="/voice")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
