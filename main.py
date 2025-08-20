from fastapi import HTTPException,status,FastAPI
from fastapi.middleware.cors import CORSMiddleware
from firebase.firebase import FirebaseRouter
from project1.project1 import Project1Router
from project2.project2 import Project2Router
from telegram.telegram import TelegramRouter
import uvicorn


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,          
    allow_methods=["*"],    
    allow_headers=["*"],
) 

app.include_router(FirebaseRouter)
app.include_router(Project1Router)
app.include_router(Project2Router)
app.include_router(TelegramRouter)


@app.get("/")
async def ping():
    print("server is on")

if __name__ == "__main__":
    uvicorn.run("main:app",host="0.0.0.0",port=8000,reload=True)

