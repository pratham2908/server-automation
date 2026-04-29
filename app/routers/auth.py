import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import get_db
from app.dependencies import get_current_profile
from app.models.profile import ProfileCreate, ProfileInDB, Token
from app.services.auth import create_access_token, get_password_hash, verify_password
from app.timezone import now_ist

router = APIRouter(
    prefix="/api/v1/auth",
    tags=["auth"],
)


@router.post("/register", response_model=Token)
async def register(profile: ProfileCreate, db: AsyncIOMotorDatabase = Depends(get_db)):
    existing_profile = await db.profiles.find_one({"email": profile.email})
    if existing_profile:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email already registered")

    profile_id = str(uuid.uuid4())
    hashed_password = get_password_hash(profile.password)

    new_profile = ProfileInDB(
        id=profile_id,
        email=profile.email,
        name=profile.name,
        password_hash=hashed_password,
        created_at=now_ist(),
        updated_at=now_ist(),
    )

    await db.profiles.insert_one(new_profile.model_dump())

    access_token = create_access_token(data={"sub": profile_id})
    return {"access_token": access_token, "token_type": "bearer"}


@router.post("/login", response_model=Token)
async def login(form_data: OAuth2PasswordRequestForm = Depends(), db: AsyncIOMotorDatabase = Depends(get_db)):
    profile = await db.profiles.find_one({"email": form_data.username})
    if not profile or not verify_password(form_data.password, profile["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token = create_access_token(data={"sub": profile["id"]})
    return {"access_token": access_token, "token_type": "bearer"}


@router.get("/me", response_model=ProfileInDB)
async def read_users_me(current_profile: ProfileInDB = Depends(get_current_profile)):
    return current_profile
