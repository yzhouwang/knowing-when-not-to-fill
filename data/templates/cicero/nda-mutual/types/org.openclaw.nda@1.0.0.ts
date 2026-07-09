/* eslint-disable @typescript-eslint/no-empty-interface */
// Generated code for namespace: org.openclaw.nda@1.0.0

// imports
import {IConcept} from './concerto@1.0.0';

// interfaces
export interface INDAData extends IConcept {
 disclosingParty: string;
 receivingParty: string;
 disclosingEntityType: EntityType;
 disclosingEntityTypeRaw?: string;
 receivingEntityType: EntityType;
 receivingEntityTypeRaw?: string;
 purpose: string;
 termMonths: number;
 noticeDays: number;
 survivalYears: number;
 governingLaw: Jurisdiction;
 governingLawRaw?: string;
 disputeForum?: DisputeForum;
 disputeForumRaw?: string;
 effectiveDate: string;
 mutual: boolean;
 hasNonCompete: boolean;
 hasNonSolicitation: boolean;
 hasResidualsClause: boolean;
 nonCompeteMonths: number;
 nonSolicitationMonths: number;
}

export enum Jurisdiction {
 Alabama = 'Alabama',
 Alaska = 'Alaska',
 Arizona = 'Arizona',
 Arkansas = 'Arkansas',
 California = 'California',
 Colorado = 'Colorado',
 Connecticut = 'Connecticut',
 Delaware = 'Delaware',
 Florida = 'Florida',
 Georgia = 'Georgia',
 Hawaii = 'Hawaii',
 Idaho = 'Idaho',
 Illinois = 'Illinois',
 Indiana = 'Indiana',
 Iowa = 'Iowa',
 Kansas = 'Kansas',
 Kentucky = 'Kentucky',
 Louisiana = 'Louisiana',
 Maine = 'Maine',
 Maryland = 'Maryland',
 Massachusetts = 'Massachusetts',
 Michigan = 'Michigan',
 Minnesota = 'Minnesota',
 Mississippi = 'Mississippi',
 Missouri = 'Missouri',
 Montana = 'Montana',
 Nebraska = 'Nebraska',
 Nevada = 'Nevada',
 New_Hampshire = 'New_Hampshire',
 New_Jersey = 'New_Jersey',
 New_Mexico = 'New_Mexico',
 New_York = 'New_York',
 North_Carolina = 'North_Carolina',
 North_Dakota = 'North_Dakota',
 Ohio = 'Ohio',
 Oklahoma = 'Oklahoma',
 Oregon = 'Oregon',
 Pennsylvania = 'Pennsylvania',
 Rhode_Island = 'Rhode_Island',
 South_Carolina = 'South_Carolina',
 South_Dakota = 'South_Dakota',
 Tennessee = 'Tennessee',
 Texas = 'Texas',
 Utah = 'Utah',
 Vermont = 'Vermont',
 Virginia = 'Virginia',
 Washington = 'Washington',
 West_Virginia = 'West_Virginia',
 Wisconsin = 'Wisconsin',
 Wyoming = 'Wyoming',
 District_of_Columbia = 'District_of_Columbia',
 Republic_of_Singapore = 'Republic_of_Singapore',
 Hong_Kong_SAR = 'Hong_Kong_SAR',
 England_and_Wales = 'England_and_Wales',
 Republic_of_Kenya = 'Republic_of_Kenya',
 Republic_of_Indonesia = 'Republic_of_Indonesia',
 Republic_of_India = 'Republic_of_India',
 Kingdom_of_Saudi_Arabia = 'Kingdom_of_Saudi_Arabia',
 United_Arab_Emirates = 'United_Arab_Emirates',
 Peoples_Republic_of_China = 'Peoples_Republic_of_China',
 Japan = 'Japan',
 Republic_of_Korea = 'Republic_of_Korea',
 Federal_Republic_of_Nigeria = 'Federal_Republic_of_Nigeria',
 Republic_of_South_Africa = 'Republic_of_South_Africa',
 OTHER = 'OTHER',
}

export enum EntityType {
 corporation = 'corporation',
 limited_liability_company = 'limited_liability_company',
 general_partnership = 'general_partnership',
 limited_partnership = 'limited_partnership',
 limited_liability_partnership = 'limited_liability_partnership',
 sole_proprietorship = 'sole_proprietorship',
 professional_corporation = 'professional_corporation',
 nonprofit_corporation = 'nonprofit_corporation',
 trust = 'trust',
 joint_venture = 'joint_venture',
 individual = 'individual',
 OTHER_ENTITY = 'OTHER_ENTITY',
}

export enum DisputeForum {
 SIAC = 'SIAC',
 ICC = 'ICC',
 LCIA = 'LCIA',
 HKIAC = 'HKIAC',
 AAA_ICDR = 'AAA_ICDR',
 CIETAC = 'CIETAC',
 DIAC = 'DIAC',
 SCC = 'SCC',
 JAMS = 'JAMS',
 OTHER_FORUM = 'OTHER_FORUM',
}
