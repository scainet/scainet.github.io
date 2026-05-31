from contextlib import asynccontextmanager
from functools import cache, lru_cache
from typing import Annotated, Any, ClassVar, Dict, List, Literal, Type, Union, cast

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, create_model


class DomainException(Exception):
    error_code: ClassVar[str] = "GENERIC_ERROR"

    def __init__(self, mensaje: str, descripcion: str):
        self.mensaje = mensaje
        self.descripcion = descripcion
        super().__init__(mensaje)



class UserNotFoundException(DomainException):
    """Cuando no se encuentra un usuario de mierda"""
    error_code = "USER_NOT_FOUND"


class UserInactiveException(DomainException):
    error_code = "USER_INACTIVE"


class BusinessRuleViolation(DomainException):
    error_code = "BUSINESS_RULE_VIOLATION"


class InsufficientFundsError(BusinessRuleViolation):
    error_code = "INSUFFICIENT_FUNDS"


class ValidationError(DomainException):
    error_code = "VALIDATION_ERROR"


class InvalidEmailError(ValidationError):
    error_code = "INVALID_EMAIL"


EXCEPTION_HTTP_MAP: Dict[Type[DomainException], int] = {
    UserNotFoundException: status.HTTP_404_NOT_FOUND,
    UserInactiveException: status.HTTP_400_BAD_REQUEST,
    BusinessRuleViolation: status.HTTP_422_UNPROCESSABLE_CONTENT,
    ValidationError: status.HTTP_422_UNPROCESSABLE_CONTENT,
}


def resolve_http_code(exc_class: Type[DomainException]) -> int:
    for cls in exc_class.__mro__:
        if cls in EXCEPTION_HTTP_MAP:
            return EXCEPTION_HTTP_MAP[cast(Type[DomainException], cls)]
    raise KeyError(
        f"No HTTP mapping found for {exc_class.__name__} in EXCEPTION_HTTP_MAP. "
        f"MRO searched: {[c.__name__ for c in exc_class.__mro__]}. "
        f"Add an entry for this class or one of its bases."
    )


@cache
def generate_error_schema(exc_class: Type[DomainException]) -> Type[BaseModel]:
    fq_name = f"{exc_class.__module__}.{exc_class.__qualname__}".replace(".", "_")
    return create_model(
        f"{fq_name}Schema",
        error_code=Literal[exc_class.error_code],  # type: ignore
        mensaje=str,
        descripcion=str,
    )


def get_responses(exceptions: List[Type[DomainException]]) -> Dict[int | str, Dict[str, Any]]:
    from itertools import groupby

    excs_with_codes = [(resolve_http_code(e), e) for e in exceptions]
    excs_with_codes.sort(key=lambda x: x[0])

    responses: Dict[int | str, Dict[str, Any]] = {}
    for http_code, group in groupby(excs_with_codes, key=lambda x: x[0]):
        schemas = [generate_error_schema(exc) for _, exc in group]
        if len(schemas) > 1:
            model = Annotated[
                Union[*schemas],  # type: ignore
                Field(discriminator="error_code"),
            ]
        else:
            model = schemas[0]
        responses[http_code] = {"model": model}
    return responses


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(lifespan=lifespan)


@app.exception_handler(DomainException)
async def domain_exception_handler(request: Request, exc: DomainException):
    http_code = resolve_http_code(type(exc))
    return JSONResponse(
        status_code=http_code,
        content={
            "error_code": exc.error_code,
            "mensaje": exc.mensaje,
            "descripcion": exc.descripcion,
        },
    )


@app.get(
    "/users/{id}",
    responses=get_responses([UserNotFoundException, UserInactiveException]),
)
async def get_user(id: str):
    if id == "404":
        raise UserNotFoundException("Not Found", "User does not exist")
    if id == "403":
        raise UserInactiveException("Inactive", "User is banned")
    return {"id": id}


@app.post(
    "/users",
    responses=get_responses([ValidationError, InvalidEmailError]),
)
async def create_user(name: str, email: str):
    if not name:
        raise ValidationError(
            "Nombre requerido",
            "El campo nombre no puede estar vacío",
        )
    if "@" not in email:
        raise InvalidEmailError(
            "Email inválido",
            f"El email {email} no contiene un @",
        )
    return {"name": name, "email": email}


@app.post(
    "/transfer",
    responses=get_responses([InsufficientFundsError, UserNotFoundException, BusinessRuleViolation]),
)
async def transfer(amount: float, sender: str, receiver: str):
    if amount > 100:
        raise InsufficientFundsError(
            "Saldo insuficiente",
            f"El monto {amount} excede el saldo disponible",
        )
    if receiver == "unknown":
        raise UserNotFoundException(
            "Receptor no encontrado",
            f"El usuario {receiver} no existe",
        )
    return {"status": "ok", "amount": amount}