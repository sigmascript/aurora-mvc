"""minimal Aurora application."""
from aurora import Aurora

app = Aurora()


def home(request):
    return "Hello from minimal!"


app.add_route("/", home)
