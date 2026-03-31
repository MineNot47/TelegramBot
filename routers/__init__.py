from .admin import router as admin_router
from .common import router as common_router
from .earn import router as earn_router
from .withdraw import router as withdraw_router

__all__ = ["common_router", "earn_router", "withdraw_router", "admin_router"]

