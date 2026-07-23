from kiro_api_proxy.credentials import parse_whoami


def test_identity_center_and_runtime_regions_are_independent():
    metadata = parse_whoami(
        """
        {
          "accountType": "IamIdentityCenter",
          "region": "ap-southeast-2",
          "startUrl": "https://example.awsapps.com/start"
        }
        Profile:
        KiroProfile-us-east-1
        arn:aws:codewhisperer:us-east-1:123456789012:profile/PROFILE1
        """
    )
    assert metadata.identity_center_region == "ap-southeast-2"
    assert metadata.runtime_region == "us-east-1"
