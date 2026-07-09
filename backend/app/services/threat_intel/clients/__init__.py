"""Clientes HTTP assíncronos para os provedores de Threat Intel."""

from .abuseipdb import AbuseIPDBClient, AbuseIPDBResult
from .otx import OTXClient, OTXResult

__all__ = ["AbuseIPDBClient", "AbuseIPDBResult", "OTXClient", "OTXResult"]
