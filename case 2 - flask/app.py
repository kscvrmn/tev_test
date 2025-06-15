import os
from base64 import b64decode
from functools import wraps

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
)
from playhouse.shortcuts import model_to_dict

app = Flask(__name__, static_folder=None)
db = PostgresqlDatabase(
    "test", user="postgres", password="postgres", host="postgres"
)


class BaseModel(Model):
    class Meta:
        database = db


class User(BaseModel):
    id = IdentityField()
    email = CharField(max_length=254, unique=True)
    tasks_number = IntegerField(default=0)


class Task(BaseModel):
    id = IdentityField()
    free = BooleanField(default=True)
    metadata = TextField()
    owner = ForeignKeyField(User, on_delete="CASCADE", backref="tasks")


@app.before_first_request
def initialize_db():
    db.connect()
    db.create_tables((User, Task))


def with_transaction(auth=True):
    def parametrized_decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            with db.manual_commit():
                db.begin()

                if auth:
                    if "user_id" not in request.args:
                        abort(401)
                    user_id = int(request.args["user_id"])
                    g.user = User.get_by_id(user_id)

                result = view(*args, **kwargs)

                db.commit()
                return result

        return wrapper

    return parametrized_decorator


def with_tasks_counter(view):
    def wrapper(*args, **kwargs):
        g.user.tasks_number += 1
        g.user.save(only=[User.tasks_number])
        return view(*args, **kwargs)

    return wrapper


@app.route("/users", methods=["POST"])
@with_transaction(auth=False)
def post_user():
    return jsonify(model_to_dict(User.create(email=request.json["email"])))


@app.route("/users/<int:user_id>", methods=["DELETE"])
@with_transaction()
def delete_user(user_id):
    if user_id != g.user.id:
        abort(401)
    g.user.delete_instance()
    return jsonify({})


@app.route("/tasks", methods=["GET"])
@with_transaction()
def get_tasks():
    task_jsons = []
    for t in Task.select():
        task_jsons.append(model_to_dict(t, recurse=True))
    return jsonify(task_jsons)


@app.route("/tasks", methods=["POST"])
@with_transaction()
@with_tasks_counter
def create_task():
    meta = request.json["meta"]
    image_bytes = b64decode(request.json["image"])

    task = Task.create(owner=g.user, metadata=meta)

    task_image_path = os.path.join(
        "storage", "task_images", str(task.id) + ".jpg"
    )
    with open(task_image_path, "wb") as f:
        f.write(image_bytes)

    return jsonify(model_to_dict(task, recurse=True))


@app.route("/tasks/<int:task_id>", methods=["GET"])
@with_transaction()
def get_task(task_id):
    return jsonify(model_to_dict(Task.get_by_id(task_id)))


@app.route("/tasks/<int:task_id>/image", methods=["GET"])
@with_transaction()
def get_task_image(task_id):
    task = Task.get_by_id(task_id)
    if task.owner.id != g.user.id:
        abort(403)
    return send_file(
        os.path.join("storage", "task_images", str(task.id) + ".jpg")
    )


@app.route("/tasks/<int:task_id>", methods=["DELETE"])
@with_transaction()
def delete_task(task_id):
    task = Task.get_by_id(task_id)
    if task.owner.id != g.user.id:
        abort(403)
    os.remove(os.path.join("storage", "task_images", str(task_id) + ".jpg"))
    Task.delete_by_id(task_id)
    return jsonify({})


@app.route("/take_free_task", methods=["GET"])
@with_transaction()
def take_free_task():
    task = (
        Task.select()
        .where((Task.owner == g.user.id) & (Task.free == True))
        .limit(1)
        .first()
    )
    if task is None:
        abort(404)
    task.free = False
    task.save(only=[Task.free])

    return jsonify(model_to_dict(task))


@app.route("/total_tasks_created", methods=["GET"])
@with_transaction()
def get_total_tasks_created():
    return jsonify(
        {"total_tasks_created": sum(u.tasks_number for u in User.select())}
    )
