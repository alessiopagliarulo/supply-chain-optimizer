from sqlalchemy import Column, Integer, String, Float, Boolean, Text
from app.core.database import Base


class Distributor(Base):
    """Real electronic components distributor with warehouse location."""
    __tablename__ = "distributors"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False, unique=True, index=True)
    # HQ/warehouse city point. NULL (with city/state/country/is_domestic NULL)
    # means the location is unknown - never fill it with a guess (migration 0010).
    latitude = Column(Float)
    longitude = Column(Float)
    city = Column(String(100))
    state = Column(String(50))
    country = Column(String(100))
    is_domestic = Column(Boolean)
    total_offers = Column(Integer, default=0)  # How many components they carry
    total_stock = Column(Integer, default=0)  # Aggregate inventory
    description = Column(Text)
