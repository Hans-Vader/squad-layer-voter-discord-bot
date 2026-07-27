import bot


ADF = [
    {"name": "M1A1", "vehType": "MBT", "spawnerSize": "MBT", "count": 1},
    {"name": "MRH-90", "vehType": "UH", "spawnerSize": "HELICOPTER", "count": 1},
    {"name": "RHIB M2", "vehType": "ULTV", "spawnerSize": "BOAT", "count": 2},
]
UNITS = {"ADF_LO_CombinedArms": ADF}


def _layer(map_id):
    return {"map_id": map_id,
            "factions": [{"factionId": "ADF", "defaultUnit": "ADF_LO_CombinedArms",
                          "availableOnTeams": [1, 2], "unitTypes": []}]}


def _names(map_id):
    return [v["name"] for v in
            bot.get_team_vehicles(_layer(map_id), "ADF", 1, "CombinedArms", UNITS)]


def test_waterless_map_drops_boats_but_keeps_helis():
    assert _names("Anvil") == ["M1A1", "MRH-90"]


def test_map_without_boats_or_helis_drops_both():
    assert _names("Fallujah") == ["M1A1"]


def test_supermod_variant_inherits_the_base_map_rule():
    assert _names("Supermod_Fallujah") == ["M1A1"]
    assert _names("GoingDark_Fallujah") == ["M1A1"]
    assert _names("Supermod_Gorodok_HalfMap") == ["M1A1", "MRH-90"]


def test_unknown_and_water_maps_are_left_alone():
    assert _names("Sanxian_Islands") == ["M1A1", "MRH-90", "RHIB M2"]
    assert _names("SomeNewMap") == ["M1A1", "MRH-90", "RHIB M2"]
