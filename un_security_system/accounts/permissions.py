def is_ict_focal(user):
    """
    Check if the user is an ICT Focal Point.

    Args:
        user: The user object to check

    Returns:
        bool: True if user is authenticated and has ICT focal role
    """
    return user.is_authenticated and (
            user.role == 'ict_focal' or
            user.is_superuser
    )


def is_lsa(user):
    """Check if user is LSA (Local System Administrator)."""
    return user.is_authenticated and (
            user.role == 'lsa' or
            user.is_superuser
    )


def is_data_entry(user):
    """Check if user is Data Entry staff."""
    return user.is_authenticated and (
            user.role == 'data_entry' or
            user.is_superuser
    )


def is_soc(user):
    """Check if user is SOC (Security Operations Center) staff."""
    return user.is_authenticated and (
            user.role == 'soc' or
            user.is_superuser
    )


def can_manage_user(request_user, target_user):
    """
    Check if request_user can manage target_user.

    Rules:
    - LSA can manage anyone
    - ICT focal can manage users in their agency only
    - Users cannot manage themselves through ICT interface

    Args:
        request_user: The user making the request
        target_user: The user being managed

    Returns:
        bool: True if management is allowed
    """
    # Superuser and LSA can manage anyone
    if request_user.is_superuser or is_lsa(request_user):
        return True

    # ICT focal can manage users in their agency (but not themselves)
    if is_ict_focal(request_user):
        return (
                request_user.agency_id and
                target_user.agency_id == request_user.agency_id and
                request_user.id != target_user.id
        )

    return False


def can_view_user(request_user, target_user):
    """
    Check if request_user can view target_user's details.

    Rules:
    - LSA can view anyone
    - ICT focal can view users in their agency
    - Users can view themselves

    Args:
        request_user: The user making the request
        target_user: The user being viewed

    Returns:
        bool: True if viewing is allowed
    """
    # Superuser and LSA can view anyone
    if request_user.is_superuser or is_lsa(request_user):
        return True

    # Users can view themselves
    if request_user.id == target_user.id:
        return True

    # ICT focal can view users in their agency
    if is_ict_focal(request_user):
        return (
                request_user.agency_id and
                target_user.agency_id == request_user.agency_id
        )

    return False