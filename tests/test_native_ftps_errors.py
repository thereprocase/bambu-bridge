"""Stable FTP application codes; no exception text is exposed to clients."""

import ftplib
import ssl

import pytest

from bambu_bridge.native_ftps import transfer_failure_code


@pytest.mark.parametrize(
    ("phase", "error", "expected"),
    [
        ("receiving_client_data", ConnectionResetError(104, "private"), "BBFTP_CLIENT_RESET"),
        ("uploading_to_printer", ConnectionResetError(104, "private"), "BBFTP_PRINTER_RESET"),
        ("receiving_client_data", TimeoutError(), "BBFTP_CLIENT_TIMEOUT"),
        ("uploading_to_printer", TimeoutError(), "BBFTP_PRINTER_TIMEOUT"),
        ("waiting_for_client_data", TimeoutError(), "BBFTP_DATA_CONNECT_TIMEOUT"),
        ("waiting_for_transfer_slot", TimeoutError(), "BBFTP_BUSY_TIMEOUT"),
        ("receiving_client_data", ssl.SSLError("private"), "BBFTP_CLIENT_TLS"),
        ("connecting_for_upload", ssl.SSLError("private"), "BBFTP_PRINTER_TLS"),
        ("uploading_to_printer", ftplib.error_temp("451 private"), "BBFTP_PRINTER_REJECTED"),
        ("transfer_size_limit", ValueError("private"), "BBFTP_SIZE_LIMIT"),
        ("receiving_client_data", ValueError("private"), "BBFTP_CLIENT_TRANSFER_ERROR"),
    ],
)
def test_failure_codes(phase, error, expected):
    assert transfer_failure_code(phase, error) == expected
