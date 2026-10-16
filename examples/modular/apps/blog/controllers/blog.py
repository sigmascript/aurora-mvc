"""Blog page controller."""
from aurora import Controller, View


class Blog(Controller):
    def get(self):
        return View("blog/index", title="Blog")
