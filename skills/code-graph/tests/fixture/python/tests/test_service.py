from app.models import User
from app.service import Service


def test_run():
    Service().run(User())
