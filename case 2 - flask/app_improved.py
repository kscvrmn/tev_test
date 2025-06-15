import os
import imghdr
from base64 import b64decode
from functools import wraps
from typing import List

from flask import Flask, abort, g, jsonify, request, send_file
from peewee import (
    BooleanField,
    CharField,
    ForeignKeyField,
    IdentityField,
    IntegerField,
    Model,
    PostgresqlDatabase,
    TextField,
    fn,
    DoesNotExist,
)
from playhouse.shortcuts import model_to_dict
from werkzeug.utils import secure_filename

# Инициализация приложения и базы данных
app = Flask(__name__, static_folder=None)
db = PostgresqlDatabase(
    "test", user="postgres", password="postgres", host="postgres"
)

# Константы
STORAGE_PATH = "storage"
IMAGE_FOLDER = os.path.join(STORAGE_PATH, "task_images")
ALLOWED_IMAGE_TYPES = {"jpg", "jpeg", "png", "gif", "bmp"}

# Создаем директорию для хранения изображений, если она не существует
os.makedirs(IMAGE_FOLDER, exist_ok=True)


class BaseModel(Model):
    """Базовая модель с подключением к БД."""
    class Meta:
        database = db


class User(BaseModel):
    """Модель пользователя."""
    id = IdentityField()
    email = CharField(max_length=254, unique=True)
    tasks_number = IntegerField(default=0)


class Task(BaseModel):
    """Модель задания."""
    id = IdentityField()
    free = BooleanField(default=True)
    metadata = TextField()
    # Добавлено поле для хранения типа изображения
    image_type = CharField(max_length=10, default="jpg")
    owner = ForeignKeyField(User, on_delete="CASCADE", backref="tasks")


# Инициализация БД при старте приложения
@app.before_first_request
def initialize_db():
    """Инициализирует соединение с БД и создает таблицы при первом запросе."""
    db.connect()
    db.create_tables((User, Task), safe=True)  # safe=True предотвращает ошибки, если таблицы уже существуют


def with_transaction(auth=True):
    """
    Декоратор для обеспечения транзакционности запросов.
    
    Args:
        auth: Флаг, указывающий нужна ли аутентификация для данного запроса
    """
    def parametrized_decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            with db.manual_commit():
                try:
                    db.begin()

                    if auth:
                        # Проверяем наличие user_id в запросе
                        if "user_id" not in request.args:
                            abort(401, description="Authentication required")
                        
                        try:
                            user_id = int(request.args["user_id"])
                        except ValueError:
                            abort(400, description="Invalid user_id format")
                        
                        try:
                            # Получаем пользователя по ID
                            g.user = User.get_by_id(user_id)
                        except DoesNotExist:
                            abort(404, description="User not found")

                    result = view(*args, **kwargs)

                    db.commit()
                    return result
                except Exception as e:
                    # В случае ошибки откатываем транзакцию
                    db.rollback()
                    # Если это не HTTP ошибка, то возвращаем 500
                    if not hasattr(e, 'code') or not isinstance(e.code, int):
                        app.logger.error(f"Unexpected error: {str(e)}")
                        abort(500, description=str(e))
                    raise e

        return wrapper

    return parametrized_decorator


def with_tasks_counter(view):
    """
    Декоратор для увеличения счетчика созданных задач пользователя.
    """
    @wraps(view)
    def wrapper(*args, **kwargs):
        # Увеличиваем счетчик задач пользователя
        g.user.tasks_number += 1
        g.user.save(only=[User.tasks_number])
        return view(*args, **kwargs)

    return wrapper


def validate_json_request(required_fields: List[str]) -> None:
    """
    Проверяет, что запрос содержит JSON и все необходимые поля.
    
    Args:
        required_fields: Список обязательных полей
        
    Raises:
        HTTPException: Если запрос не содержит JSON или отсутствуют обязательные поля
    """
    if not request.is_json:
        abort(400, description="Content-Type must be application/json")
    
    for field in required_fields:
        if field not in request.json:
            abort(400, description=f"Missing required field: {field}")


@app.route("/users", methods=["POST"])
@with_transaction(auth=False)
def post_user():
    """Создает нового пользователя."""
    validate_json_request(["email"])
    
    email = request.json["email"]
    
    # Проверка формата email (простая)
    if "@" not in email or "." not in email:
        abort(400, description="Invalid email format")
    
    try:
        # Создаем пользователя
        user = User.create(email=email)
        return jsonify(model_to_dict(user)), 201  # 201 Created
    except Exception as e:
        # Обрабатываем ошибку уникальности email
        if "unique" in str(e).lower():
            abort(409, description="Email already exists")
        raise


@app.route("/users/<int:user_id>", methods=["DELETE"])
@with_transaction()
def delete_user(user_id):
    """Удаляет пользователя по ID."""
    # Проверка, что пользователь удаляет сам себя
    if user_id != g.user.id:
        abort(403, description="You can only delete your own account")  # 403 Forbidden вместо 401
    
    g.user.delete_instance(recursive=True)  # recursive=True удаляет связанные записи
    return "", 204  # 204 No Content


@app.route("/tasks", methods=["GET"])
@with_transaction()
def get_tasks():
    """Возвращает список заданий текущего пользователя."""
    # Используем JOIN для избежания N+1 проблемы
    query = (Task
             .select(Task, User)
             .join(User)
             .where(Task.owner == g.user))
    
    task_jsons = []
    for task in query:
        # Преобразуем модель в словарь, исключая некоторые поля
        task_dict = model_to_dict(task, recurse=True, exclude=[User.tasks_number])
        task_jsons.append(task_dict)
    
    return jsonify(task_jsons)


@app.route("/tasks", methods=["POST"])
@with_transaction()
@with_tasks_counter
def create_task():
    """Создает новое задание с прикрепленным изображением."""
    validate_json_request(["meta", "image"])
    
    meta = request.json["meta"]
    
    # Декодируем base64 изображение
    try:
        image_bytes = b64decode(request.json["image"])
    except Exception:
        abort(400, description="Invalid base64 image")
    
    # Определяем тип изображения
    img_type = imghdr.what(None, h=image_bytes)
    if not img_type:
        abort(400, description="Invalid image format")
    if img_type not in ALLOWED_IMAGE_TYPES:
        abort(400, description=f"Image type {img_type} not allowed")
    
    # Создаем задание
    task = Task.create(owner=g.user, metadata=meta, image_type=img_type)
    
    # Генерируем уникальное имя файла
    filename = f"{task.id}.{img_type}"
    task_image_path = os.path.join(IMAGE_FOLDER, filename)
    
    # Сохраняем изображение
    try:
        with open(task_image_path, "wb") as f:
            f.write(image_bytes)
    except IOError as e:
        # В случае ошибки удаляем созданное задание
        task.delete_instance()
        abort(500, description=f"Failed to save image: {str(e)}")
    
    return jsonify(model_to_dict(task, recurse=True)), 201  # 201 Created


@app.route("/tasks/<int:task_id>", methods=["GET"])
@with_transaction()
def get_task(task_id):
    """Возвращает информацию о задании по ID."""
    try:
        # Получаем задание и проверяем права доступа
        task = Task.get_by_id(task_id)
        if task.owner.id != g.user.id:
            abort(403, description="Access denied")
        
        return jsonify(model_to_dict(task))
    except DoesNotExist:
        abort(404, description="Task not found")


@app.route("/tasks/<int:task_id>/image", methods=["GET"])
@with_transaction()
def get_task_image(task_id):
    """Возвращает изображение задания по ID."""
    try:
        # Получаем задание и проверяем права доступа
        task = Task.get_by_id(task_id)
        if task.owner.id != g.user.id:
            abort(403, description="Access denied")
        
        # Формируем путь к изображению
        image_path = os.path.join(IMAGE_FOLDER, f"{task.id}.{task.image_type}")
        
        # Проверяем существование файла
        if not os.path.exists(image_path):
            abort(404, description="Image not found")
        
        # Отправляем файл с правильным MIME типом
        return send_file(
            image_path,
            mimetype=f"image/{task.image_type}"
        )
    except DoesNotExist:
        abort(404, description="Task not found")


@app.route("/tasks/<int:task_id>", methods=["DELETE"])
@with_transaction()
def delete_task(task_id):
    """Удаляет задание по ID."""
    try:
        # Получаем задание и проверяем права доступа
        task = Task.get_by_id(task_id)
        if task.owner.id != g.user.id:
            abort(403, description="Access denied")
        
        # Удаляем изображение
        image_path = os.path.join(IMAGE_FOLDER, f"{task.id}.{task.image_type}")
        try:
            if os.path.exists(image_path):
                os.remove(image_path)
        except OSError as e:
            app.logger.error(f"Failed to delete image: {str(e)}")
            # Продолжаем выполнение даже при ошибке удаления файла
        
        # Удаляем задание
        task.delete_instance()
        return "", 204  # 204 No Content
    except DoesNotExist:
        abort(404, description="Task not found")


@app.route("/take_free_task", methods=["GET"])
@with_transaction()
def take_free_task():
    """Возвращает и помечает занятым свободное задание."""
    # Атомарно получаем и помечаем задание как занятое
    with db.atomic():
        task = (
            Task.select()
            .where(Task.free == True)  # Исправлено: ищем любые свободные задания
            .limit(1)
            .first()
        )
        
        if task is None:
            abort(404, description="No free tasks available")
        
        # Помечаем задание как занятое
        task.free = False
        task.save(only=[Task.free])
    
    return jsonify(model_to_dict(task))


@app.route("/total_tasks_created", methods=["GET"])
@with_transaction()
def get_total_tasks_created():
    """Возвращает общее количество созданных заданий."""
    # Используем агрегацию на стороне БД для эффективности
    total = User.select(fn.SUM(User.tasks_number)).scalar() or 0
    return jsonify({"total_tasks_created": total})


# Обработчики ошибок
@app.errorhandler(400)
def bad_request(e):
    return jsonify({"error": str(e.description)}), 400

@app.errorhandler(401)
def unauthorized(e):
    return jsonify({"error": str(e.description)}), 401

@app.errorhandler(403)
def forbidden(e):
    return jsonify({"error": str(e.description)}), 403

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": str(e.description)}), 404

@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": str(e.description)}), 500 