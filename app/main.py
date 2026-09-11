from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Field, Session, SQLModel, col, create_engine, select

templates = Jinja2Templates(directory="app/templates")
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


def get_todos() -> list[Todo]:
    with Session(engine) as session:
        return list(session.exec(select(Todo).order_by(col(Todo.id))).all())


def render_todo_list(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="_todo_list.html",
        context={"todos": get_todos()},
    )


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"todos": get_todos()},
    )


@app.post("/todos", response_class=HTMLResponse)
async def create_todo(request: Request, title: str = Form(...)) -> HTMLResponse:
    cleaned_title = title.strip()
    if cleaned_title:
        with Session(engine) as session:
            session.add(Todo(title=cleaned_title))
            session.commit()

    return render_todo_list(request)


@app.patch("/todos/{todo_id}/toggle", response_class=HTMLResponse)
async def toggle_todo(request: Request, todo_id: int) -> HTMLResponse:
    with Session(engine) as session:
        todo = session.get(Todo, todo_id)
        if todo is not None:
            todo.completed = not todo.completed
            session.add(todo)
            session.commit()

    return render_todo_list(request)


@app.delete("/todos/{todo_id}", response_class=HTMLResponse)
async def delete_todo(request: Request, todo_id: int) -> HTMLResponse:
    with Session(engine) as session:
        todo = session.get(Todo, todo_id)
        if todo is not None:
            session.delete(todo)
            session.commit()

    return render_todo_list(request)
