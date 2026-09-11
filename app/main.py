from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlmodel import Field, Session, SQLModel, col, create_engine, select

engine = create_engine("sqlite:///todos.db", connect_args={"check_same_thread": False})


class Todo(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    title: str
    completed: bool = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    SQLModel.metadata.create_all(engine)
    yield


app = FastAPI(title="Todo API", lifespan=lifespan)


@app.get("/")
def get_todos() -> list[Todo]:
    with Session(engine) as session:
        return list(session.exec(select(Todo).order_by(col(Todo.id))).all())


@app.post("/todos", response_model=Todo)
async def create_todo(todo: Todo) -> Todo:
    with Session(engine) as session:
        session.add(todo)
        session.commit()
        session.refresh(todo)

        return todo
