/* eslint-disable @typescript-eslint/no-empty-interface */
// Generated code for namespace: org.openclaw.jv@1.0.0

// imports
import {IConcept} from './concerto@1.0.0';

// interfaces
export interface IJointVentureData extends IConcept {
 partyAName: string;
 partyALegalRep: string;
 partyAAddress: string;
 partyAContact: string;
 partyBName: string;
 partyBLegalRep: string;
 partyBAddress: string;
 partyBContact: string;
 jvCompanyName: string;
 registeredCapital: string;
 registrationPlace: string;
 businessScope: string;
 operationTermYears: number;
 partyAEquityPercent: number;
 partyBEquityPercent: number;
 partyAContributionAmount: string;
 partyBContributionAmount: string;
 contributionCurrency: string;
 initialContributionPercent: number;
 contributionDeadlineDays: number;
 firstPaymentPercent: number;
 firstPaymentTrigger: string;
 totalDirectors: number;
 partyADirectors: number;
 partyBDirectors: number;
 chairmanNominatedBy: string;
 profitDistributionDays: number;
 dailyPenaltyRate: string;
 breachCurePeriodDays: number;
 confidentialityPenaltyAmount: string;
 governingLaw: string;
 arbitrationBody: string;
 disputeResolutionRules: string;
 operatingRegion: string;
 applicableCompanyLaw: string;
 noticeChangeBusinessDays: number;
 localComplianceLaws?: string[];
 bilingualEnglishPrevails: boolean;
 effectiveDate: string;
}
