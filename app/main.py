import asyncio
import signal
import threading
from collections.abc import AsyncIterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from os import getenv
from types import FrameType
from typing import Any, Literal
from uuid import uuid4

from fastapi import FastAPI, Form, Header, Request
from fastapi.responses import HTMLResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.templating import Jinja2Templates
from sqlmodel import Field, Session, SQLModel, col, create_engine, select

templates = Jinja2Templates(directory="app/templates")
database_url = getenv("TODO_DATABASE_URL", "sqlite:///todos.db")
engine = create_engine(database_url, connect_args={"check_same_thread": False})


# 接続(タブ)あたり、未送信のまま溜めておける通知の上限
MAX_PENDING_EVENTS_PER_CLIENT = 100


class Todo(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    title: str
    completed: bool = False


@dataclass(frozen=True)
class TodoEvent:
    action: Literal["created", "completed", "reopened", "deleted"]
    title: str


class TodoEventBroker:
    """接続中のブラウザ(タブ)ごとにキューを持ち、Todo の変更を配信する。

    プロセス内メモリで管理するため、ワーカーが 1 つの構成を前提とする。
    """

    def __init__(self) -> None:
        self._subscribers: dict[asyncio.Queue[TodoEvent], str] = {}

    def subscribe(self, client_id: str) -> asyncio.Queue[TodoEvent]:
        queue: asyncio.Queue[TodoEvent] = asyncio.Queue(
            maxsize=MAX_PENDING_EVENTS_PER_CLIENT
        )
        self._subscribers[queue] = client_id
        return queue

    def unsubscribe(self, queue: asyncio.Queue[TodoEvent]) -> None:
        self._subscribers.pop(queue, None)

    def publish(self, event: TodoEvent, sender_id: str | None) -> None:
        for queue, client_id in self._subscribers.items():
            # 操作した本人には通知しない
            if client_id == sender_id:
                continue
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass  # 受信が滞っているクライアントの通知は捨てる

    def close(self) -> None:
        for queue in self._subscribers:
            queue.shutdown(immediate=True)


broker = TodoEventBroker()


def close_broker_on_exit_signal() -> None:
    """終了シグナルで SSE ストリームを閉じる。

    SSE の接続は切れないため、閉じないとサーバーが
    "Waiting for connections to close" のまま停止しない(リロード時も同様)。
    サーバー側のシグナルハンドラは呼び出しを引き継ぐ。
    """
    if threading.current_thread() is not threading.main_thread():
        return

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous = signal.getsignal(sig)

        def handler(
            signum: int, frame: FrameType | None, previous: Any = previous
        ) -> None:
            loop.call_soon_threadsafe(broker.close)
            if callable(previous):
                previous(signum, frame)

        signal.signal(sig, handler)


@asynccontextmanager
async def lifespan(app: FastAPI):
    SQLModel.metadata.create_all(engine)
    close_broker_on_exit_signal()
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
        # タブごとに ID を振り、自分の操作による通知を受け取らないようにする
        context={"todos": get_todos(), "client_id": uuid4().hex},
    )


@app.get("/todos", response_class=HTMLResponse)
async def list_todos(request: Request) -> HTMLResponse:
    return render_todo_list(request)


@app.get("/todos/events", response_class=EventSourceResponse)
async def todo_events(client_id: str) -> AsyncIterable[ServerSentEvent]:
    queue = broker.subscribe(client_id)
    try:
        while True:
            try:
                event = await queue.get()
            except asyncio.QueueShutDown:
                return  # サーバー停止時。ブラウザは再起動後に自動で再接続する
            yield ServerSentEvent(
                event="todo-changed",
                raw_data=templates.get_template("_todo_toast.html").render(event=event),
            )
    finally:
        broker.unsubscribe(queue)


@app.post("/todos", response_class=HTMLResponse)
async def create_todo(
    request: Request,
    title: str = Form(...),
    x_client_id: str | None = Header(default=None),
) -> HTMLResponse:
    cleaned_title = title.strip()
    if cleaned_title:
        with Session(engine) as session:
            session.add(Todo(title=cleaned_title))
            session.commit()
        broker.publish(TodoEvent("created", cleaned_title), x_client_id)

    return render_todo_list(request)


@app.patch("/todos/{todo_id}/toggle", response_class=HTMLResponse)
async def toggle_todo(
    request: Request,
    todo_id: int,
    x_client_id: str | None = Header(default=None),
) -> HTMLResponse:
    with Session(engine) as session:
        todo = session.get(Todo, todo_id)
        if todo is not None:
            todo.completed = not todo.completed
            event = TodoEvent("completed" if todo.completed else "reopened", todo.title)
            session.add(todo)
            session.commit()
            broker.publish(event, x_client_id)

    return render_todo_list(request)


@app.delete("/todos/{todo_id}", response_class=HTMLResponse)
async def delete_todo(
    request: Request,
    todo_id: int,
    x_client_id: str | None = Header(default=None),
) -> HTMLResponse:
    with Session(engine) as session:
        todo = session.get(Todo, todo_id)
        if todo is not None:
            event = TodoEvent("deleted", todo.title)
            session.delete(todo)
            session.commit()
            broker.publish(event, x_client_id)

    return render_todo_list(request)
