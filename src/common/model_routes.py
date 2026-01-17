import json


class UnknownModelRoute(Exception):
    pass


class ModelRoutes:
    """
    This class provides methods to get/modify model routes.

    It abstracts the communication between the router and the routes DB
    for easier testing and modifying the implementation details.
    """

    def __init__(self):
        # This will be replaced by a Redis DB in the future
        self.LOCAL_MODEL_ROUTES_FILE_PATH = "../../model_routes.json"

    async def get_route(self, model_name: str) -> str:
        try:
            with open(self.LOCAL_MODEL_ROUTES_FILE_PATH) as f:
                routes_str = f.read()
                routes_dict = json.loads(routes_str)

                if model_name not in routes_dict:
                    raise UnknownModelRoute(f"Unknown model: '{model_name}'")

                route = routes_dict[model_name]
                return route
        except FileNotFoundError:
            raise UnknownModelRoute(f"Unknown model: '{model_name}'")
