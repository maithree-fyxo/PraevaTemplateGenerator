"""
Mock assignment data mirroring the sample values in the supplied template.

Used to develop & test the PPTX engine before the live Ezekia API is wired.
When the real API is connected, ezekia.py produces the same Assignment shape.
"""
from .models import Assignment, Candidate, CareerEntry, CareerGroup, Stage


def _c(company, role, dates):
    # kept as a (company, role, dates) tuple; grouped into CareerGroups below
    return (company, role, dates)


def _group(rows):
    """Group consecutive same-company (company, role, dates) tuples into CareerGroups."""
    groups = []
    for company, role, dates in rows:
        entry = CareerEntry(role=role, dates=dates)
        if groups and groups[-1].company.strip().lower() == company.strip().lower():
            groups[-1].roles.append(entry)
        else:
            groups.append(CareerGroup(company=company, roles=[entry]))
    return groups


def mock_assignment() -> Assignment:
    a = Assignment(
        name="Campfire",
        title="Search Update – Campfire",
        prepared_for=[
            "Prepared for: Joe Gradwell, Alex Brown, Jim Brigden (Campfire)",
            "Will Baker (Literacy Capital)",
        ],
        date="13/08/2026",
        candidates=[
            # -------- Engaged (full profiles) --------
            Candidate(
                name="Will Engert", stage=Stage.ENGAGED, has_profile=True,
                name_url="https://www.linkedin.com/in/will-engert",
                salary="£220,000 base, bonus, LTIP",
                location="London, open to commuting",
                availability="4 months",
                education="ICAEW\t2011 - 2014\nACA, Accounting",
                career=[
                    _c("Publicis Groupe", "Chief Commercial Officer, Media", "2025 - P"),
                    _c("Publicis Groupe", "Managing Director", "2024 - 2025"),
                    _c("Independent Consultant", "Finance, Operational & Commercial Consultancy", "2024 - 2025"),
                    _c("The & Partnership", "Chief Operating Officer & Partner", "2020 - 2024"),
                    _c("UK Government", "Project Director at FCDO", "2019 - 2020"),
                    _c("The & Partnership", "Commercial Finance Director", "2016 - 2019"),
                    _c("WPP", "Mergers and Acquisitions Director", "2016 - 2016"),
                    _c("Deloitte", "Manager", "2011 - 2016"),
                ],
            ),
            Candidate(
                name="Jess Markwood", stage=Stage.ENGAGED, has_profile=True,
                salary="£150,000, 40% bonus",
                location="London, open to relocating",
                availability="Immediately available",
                education="Falmouth College of Art\t2003 - 2004\nFoundation Fine Art, Fashion and Embellishment",
                career=[
                    _c("Independent Consultant", "Fractional COO", "2025 - present"),
                    _c("The Fifth Group", "Chief Operating Officer", "2022 - 2025"),
                    _c("The Studio London", "Content & Client Director", "2016 - 2019"),
                    _c("Mode Media Corporation", "Content Director", "2014 - 2016"),
                    _c("Dorothy Perkins, Arcadia", "Digital Lead", "2012 - 2013"),
                    _c("Arcadia Retail Group", "Group Ecommerce", "2012 - 2013"),
                ],
            ),
            Candidate(
                name="Sophie Wooller Dent", stage=Stage.ENGAGED, has_profile=True,
                salary="£180,000 base, up to 20% bonus",
                location="London, open to commuting",
                availability="6 months",
                education="Durham University\t2004 - 2007\nBA, History",
                career=[
                    _c("Zenith UK", "Chief Operating Officer", "2025 - present"),
                    _c("Croud", "Chief Operating Officer", "2020 - 2025"),
                    _c("FareShare UK", "Project Finance Business Partner", "2020 - 2020"),
                    _c("The Programmatic Advisory", "Head of Operations", "2019 - 2020"),
                    _c("Dentsu Aegis Network", "Strategy Director", "2018 - 2019"),
                    _c("iProspect", "Director of Data and Analytics", "2015 - 2018"),
                    _c("BT", "Finance Manager", "2013 - 2015"),
                ],
            ),
            # -------- Pipeline (table) --------
            Candidate(name="Ed Turner", stage=Stage.PIPELINE, role="Managing Director", company="Bicycle", status="In conversation"),
            Candidate(name="Geoff Griffiths", stage=Stage.PIPELINE, role="Chief Commercial Officer", company="Brave Bison", status="In conversation"),
            Candidate(name="Stuart Hogg", stage=Stage.PIPELINE, role="Chief Operating Officer, UK&I", company="Dentsu", status="In conversation"),
            Candidate(name="Jade Raad", stage=Stage.PIPELINE, role="Managing Director", company="Jungle Creations", status="In conversation"),
            Candidate(name="Vincent Rebeix", stage=Stage.PIPELINE, role="EMEA Chief Operating Officer", company="Omnicom Media", status="In conversation"),
            # -------- Target: filled manually in the deck (placeholders) --------
            # -------- Discounted profile ("Praeva discounted") --------
            Candidate(
                name="Luke Bristow", stage=Stage.DISCOUNTED, has_profile=True,
                role="President, EMEA", company="Dexerto", status="Waiting for exit",
                salary="£175,000 base, up to 50% bonus, share options",
                location="London", availability="TBC",
                education="University of Exeter\nBSc Hons, Psychology",
                career=[
                    _c("Dexerto", "President, EMEA", "2026 - present"),
                    _c("MNC", "Chief Executive Officer", "2024 - 2026"),
                    _c("NewGen", "Co-Owner and Chief Operating Officer", "2019 - 2024"),
                    _c("The Honey Partnership", "Founding Partner", "2017 - 2019"),
                    _c("Ogilvy UK", "Account Director", "2013 - 2017"),
                    _c("Eight Partnership", "Account Manager", "2012 - 2013"),
                ],
            ),
            Candidate(
                name="Priya Anand", stage=Stage.DISCOUNTED, has_profile=True,
                name_url="https://www.linkedin.com/in/priya-anand",
                role="Chief Operating Officer", company="Northgate", status="Counter-offered",
                salary="£165,000 base, 30% bonus",
                location="Manchester", availability="3 months",
                education="University of Manchester\t2005 - 2008\nBSc, Economics",
                career=[
                    _c("Northgate", "Chief Operating Officer", "2022 - P"),
                    _c("Northgate", "Operations Director", "2020 - 2022"),
                    _c("Sky", "Head of Ops", "2016 - 2020"),
                ],
            ),
            # -------- Discounted table ("Not Interested") --------
            Candidate(name="Lee Avery-Jones", stage=Stage.DISCOUNTED, role="Chief Operating Officer", company="Amplify", status="Not open to a move"),
            Candidate(name="Rhoda Sell", stage=Stage.DISCOUNTED, role="Chief Operating Officer", company="Automated Creative", status="Waiting for exit"),
            Candidate(name="Lucy Barnes", stage=Stage.DISCOUNTED, role="Deputy Managing Director", company="Havas Market UK", status="Not open to a move"),
            Candidate(name="Simon Bevan", stage=Stage.DISCOUNTED, role="Chief Operating Officer", company="Havas Media Network", status="Not open to location"),
            Candidate(name="Daniel Paget", stage=Stage.DISCOUNTED, role="Chief Operating Officer", company="IDHL", status="Not open to a move"),
        ],
    )
    # convert each profile candidate's flat career list into company groups
    for c in a.candidates:
        if c.career:
            c.career = _group(c.career)
    return a
