"""Three short synthetic two-speaker primary-care consults (about one minute each).

Each one matches a synthetic patient in sample_fhir_data.py. Between them they cover:
a negated symptom, a family-history mention, a medication change, and an uncertain
("could be") diagnosis. The clinician always speaks first (the parser labels the
first speaker "Clinician").
"""

CONSULTS = [
    {
        "file": "consult-01-knee-pain-blood-pressure.wav",
        "patient": "Elena Rodriguez (0725e407)",
        "voices": {"C": "Matthew", "P": "Joanna"},
        "turns": [
            ("C", "Good morning, Ms. Rodriguez. What brings you in today?"),
            ("P", "My right knee has been aching for about three weeks. It is worse on the stairs."),
            ("C", "Any fever, or redness around the knee?"),
            ("P", "No fever, and no redness. Maybe a little swelling in the evenings."),
            ("C", "How has your blood pressure been at home?"),
            ("P", "Around one forty-five over ninety most mornings."),
            ("C", "That is still high, so your hypertension is not well controlled. "
                  "Any chest pain or shortness of breath?"),
            ("P", "No chest pain, and I am not short of breath."),
            ("C", "Does anyone in your family have heart disease?"),
            ("P", "My father had a heart attack when he was sixty."),
            ("C", "Your knee pain is most likely your osteoarthritis flaring up. For your blood pressure, "
                  "I would like to start amlodipine five milligrams once a day, and you should keep taking "
                  "atorvastatin ten milligrams at night."),
            ("P", "Can I take ibuprofen for the knee?"),
            ("C", "Please use acetaminophen five hundred milligrams up to three times a day instead. "
                  "We will recheck your blood pressure in four weeks."),
        ],
    },
    {
        "file": "consult-02-gout-follow-up.wav",
        "patient": "Marcia Oliveria (69834515)",
        "voices": {"C": "Matthew", "P": "Ruth"},
        "turns": [
            ("C", "Hello, Mrs. Oliveria. How is your right wrist since the gout flare?"),
            ("P", "Much better. The swelling went down after the colchicine."),
            ("C", "Any new joint pain in your feet or knees?"),
            ("P", "No, nothing new."),
            ("C", "Your uric acid was high last time. Now that the flare has settled, I want to stop the "
                  "colchicine and start allopurinol one hundred milligrams once a day."),
            ("P", "Will that be safe with my other medicines?"),
            ("C", "Yes, it is fine with your aspirin and your blood pressure tablets. "
                  "You mentioned feeling tired lately?"),
            ("P", "Yes, and I get dizzy when I stand up quickly."),
            ("C", "That could be from your blood pressure medicine, or it could be mild anemia. "
                  "We will check your blood count today."),
            ("P", "Should I keep drinking plenty of water?"),
            ("C", "Yes, plenty of water, and please limit alcohol. I will see you again in six weeks."),
        ],
    },
    {
        "file": "consult-03-cough.wav",
        "patient": "Diego Ramirez (46383dd7)",
        "voices": {"C": "Joanna", "P": "Stephen"},
        "turns": [
            ("C", "Hi, Mr. Ramirez. What is going on today?"),
            ("P", "I have had a cough and a sore throat for five days."),
            ("C", "Any fever or chills?"),
            ("P", "A low fever the first two days, but none since then."),
            ("C", "Are you coughing anything up?"),
            ("P", "Some yellow phlegm in the mornings."),
            ("C", "Any wheezing or chest tightness?"),
            ("P", "No wheezing at all."),
            ("C", "Does anyone in your family have asthma?"),
            ("P", "My sister has asthma, and my mother has type two diabetes."),
            ("C", "Your lungs sound clear. This could be acute bronchitis, most likely viral, so an "
                  "antibiotic will not help. Drink plenty of fluids, and you can take guaifenesin six hundred "
                  "milligrams twice a day."),
            ("P", "Should I keep using the chlorhexidine mouthwash from my dentist?"),
            ("C", "You can stop the chlorhexidine now that the dental infection has healed."),
        ],
    },
]
