"""Home page controller."""
from aurora import Controller, View


class Home(Controller):
    def get(self):
        return View("home", title="Welcome to mvc")
